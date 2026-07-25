/**
 * ImportTab — the "Excel import" section of a media plan.
 *
 * - Per-sheet import contracts list.
 * - Contract editor (JSON).
 * - Upload an .xlsx file → base64 → full report.
 * - Report: per-line created/updated/unchanged, rejections WITH reasons, the
 *   {file, imported, rejected} totals highlighted (the invariant).
 * - "Publish version" action after a successful import.
 *
 * Restyled onto the v3 design system (imports.css). AD-15: everything goes
 * through the API — no direct DB access. Client-side 15 MB cap.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { ImportContract, ImportReport, MediaPlanDetail } from "./types";
import "./imports.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_FILE_BYTES = 15 * 1024 * 1024; // 15 MB

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function fmtMoney(amount: number, currency: string): string {
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(amount);
}

/** Read a File as base64 (without the data:... prefix). */
async function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      // result = "data:...;base64,AAAA..."
      const b64 = result.split(",")[1] ?? "";
      resolve(b64);
    };
    reader.onerror = () => reject(new Error("Failed to read the file."));
    reader.readAsDataURL(file);
  });
}

const STATUS_CHIP: Record<string, string> = {
  created: "success",
  updated: "warning",
  rejected: "error",
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ImportTabProps {
  plan: MediaPlanDetail;
  apiBase?: string;
  /**
   * E3-F-3: called after a successful publish so the parent page reloads the
   * plan (otherwise the displayed active version stays stale post-publish until
   * the user refreshes manually).
   */
  onRefresh?: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function ImportTab({ plan, apiBase = "", onRefresh }: ImportTabProps) {
  const [contracts, setContracts] = useState<ImportContract[]>([]);
  const [contractsLoading, setContractsLoading] = useState(true);
  const [contractsError, setContractsError] = useState<string | null>(null);

  // Contract editing
  const [editingSheet, setEditingSheet] = useState<string | null>(null);
  const [contractJson, setContractJson] = useState<string>("");
  const [contractSaving, setContractSaving] = useState(false);
  const [contractSaveError, setContractSaveError] = useState<string | null>(null);
  const [contractSaveOk, setContractSaveOk] = useState(false);

  // File import
  const [fileError, setFileError] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [importReport, setImportReport] = useState<ImportReport | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Publishing the imported version
  const [publishingVersionId, setPublishingVersionId] = useState<string | null>(null);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [publishSuccess, setPublishSuccess] = useState(false);

  // ---------------------------------------------------------------------------
  // Load contracts
  // ---------------------------------------------------------------------------

  const loadContracts = useCallback(async () => {
    setContractsLoading(true);
    setContractsError(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/mediaplans/${encodeURIComponent(plan.id)}/import-contracts`
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.message ?? `HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as { contracts: ImportContract[] };
      setContracts(data.contracts ?? []);
    } catch (err) {
      setContractsError(err instanceof Error ? err.message : String(err));
    } finally {
      setContractsLoading(false);
    }
  }, [plan.id, apiBase]);

  useEffect(() => {
    void loadContracts();
  }, [loadContracts]);

  // ---------------------------------------------------------------------------
  // Contract editing
  // ---------------------------------------------------------------------------

  function handleEditContract(contract: ImportContract) {
    setEditingSheet(contract.sheet_name);
    setContractJson(JSON.stringify(contract.contract, null, 2));
    setContractSaveError(null);
    setContractSaveOk(false);
  }

  function handleNewContract() {
    setEditingSheet("__new__");
    setContractJson(
      JSON.stringify(
        {
          sheet_name: "",
          header_row: 1,
          line_key: "label",
          date_format: "%Y-%m-%d",
          columns: {
            label: "Label",
            support: "Support",
            date_debut: "Start date",
            date_fin: "End date",
            budget: "Budget",
          },
          is_plan_only: false,
        },
        null,
        2
      )
    );
    setContractSaveError(null);
    setContractSaveOk(false);
  }

  async function handleSaveContract(sheetName: string) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(contractJson);
    } catch {
      setContractSaveError("Invalid JSON — fix the syntax before saving.");
      return;
    }

    const effectiveSheet =
      editingSheet === "__new__"
        ? ((parsed as Record<string, unknown>).sheet_name as string) ?? sheetName
        : sheetName;

    if (!effectiveSheet?.trim()) {
      setContractSaveError("The sheet name (sheet_name) is required.");
      return;
    }

    setContractSaving(true);
    setContractSaveError(null);
    setContractSaveOk(false);
    try {
      const resp = await fetch(
        `${apiBase}/api/mediaplans/${encodeURIComponent(plan.id)}/import-contracts/${encodeURIComponent(effectiveSheet.trim())}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ contract: parsed }),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setContractSaveError(data?.message ?? `HTTP ${resp.status}.`);
        return;
      }
      setContractSaveOk(true);
      setEditingSheet(null);
      await loadContracts();
    } catch (err) {
      setContractSaveError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setContractSaving(false);
    }
  }

  // ---------------------------------------------------------------------------
  // File import
  // ---------------------------------------------------------------------------

  async function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    setFileError(null);
    setImportReport(null);
    setImportError(null);
    setPublishError(null);
    setPublishSuccess(false);

    if (!file) return;

    // Client-side 15 MB cap
    if (file.size > MAX_FILE_BYTES) {
      setFileError(
        `File too large: ${(file.size / 1024 / 1024).toFixed(1)} MB (max 15 MB).`
      );
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }

    setImporting(true);
    try {
      const base64 = await fileToBase64(file);
      const resp = await fetch(
        `${apiBase}/api/mediaplans/${encodeURIComponent(plan.id)}/import`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ file_base64: base64 }),
        }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setImportError(data?.message ?? `HTTP ${resp.status} during import.`);
        return;
      }
      const report = (await resp.json()) as ImportReport;
      setImportReport(report);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : "Unexpected error during import.");
    } finally {
      setImporting(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  // ---------------------------------------------------------------------------
  // Publish
  // ---------------------------------------------------------------------------

  async function handlePublishVersion() {
    if (!importReport?.version?.id) return;
    const versionId = importReport.version.id;
    setPublishingVersionId(versionId);
    setPublishError(null);
    try {
      const resp = await fetch(
        `${apiBase}/api/mediaplans/versions/${encodeURIComponent(versionId)}/publish`,
        { method: "POST", headers: { "Content-Type": "application/json" } }
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setPublishError(data?.message ?? `HTTP ${resp.status} while publishing.`);
        return;
      }
      setPublishSuccess(true);
      // E3-F-3: the active version just changed → ask the parent to reload.
      onRefresh?.();
    } catch (err) {
      setPublishError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setPublishingVersionId(null);
    }
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div>
      {/* ------------------------------------------------------------------ */}
      {/* Import contracts                                                     */}
      {/* ------------------------------------------------------------------ */}
      <section className="imports-section">
        <div className="imports-line-head" style={{ justifyContent: "space-between" }}>
          <h3 className="imports-section-title" style={{ margin: 0 }}>
            Per-sheet import contracts
          </h3>
          <button
            className="secondary-button"
            type="button"
            onClick={handleNewContract}
            data-testid="new-contract-btn"
          >
            New contract
          </button>
        </div>

        {contractsError && (
          <div className="imports-alert error" role="alert" data-testid="contracts-error">
            {contractsError}
          </div>
        )}
        {contractSaveOk && (
          <div className="imports-alert success" role="status">
            Contract saved.
          </div>
        )}

        {contractsLoading ? (
          <div className="imports-inline-state">
            <span className="imports-spinner" aria-hidden="true" />
            Loading contracts…
          </div>
        ) : contracts.length === 0 ? (
          <p className="imports-section-note" data-testid="contracts-empty">
            No import contract defined yet. Create one to configure how your Excel sheets are read.
          </p>
        ) : (
          <div className="panel imports-panel">
            <div className="table-scroll" tabIndex={0} aria-label="Import contracts">
              <table className="imports-table" data-testid="contracts-table">
                <thead>
                  <tr>
                    <th>Sheet</th>
                    <th>Line key</th>
                    <th>Updated</th>
                    <th>
                      <span className="sr-only">Action</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {contracts.map((c) => (
                    <tr key={c.id} data-testid={`contract-row-${c.sheet_name}`}>
                      <td>
                        <span className="imports-cell-strong">{c.sheet_name}</span>
                      </td>
                      <td className="imports-cell-muted">
                        {((c.contract as Record<string, unknown>).line_key as string) ?? "—"}
                      </td>
                      <td className="imports-cell-muted">{fmtDate(c.updated_at)}</td>
                      <td style={{ textAlign: "right" }}>
                        <button
                          className="quiet-button"
                          type="button"
                          onClick={() => handleEditContract(c)}
                          data-testid={`edit-contract-${c.sheet_name}`}
                        >
                          Edit
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Contract editor */}
        {editingSheet !== null && (
          <div className="imports-editor" data-testid="contract-editor">
            <h3>
              {editingSheet === "__new__" ? "New contract" : `Edit: ${editingSheet}`}
            </h3>
            <label className="imports-field">
              <span className="imports-label">Contract JSON</span>
              <textarea
                className="imports-textarea"
                value={contractJson}
                onChange={(e) => setContractJson(e.target.value)}
                data-testid="contract-json-editor"
                aria-label="Import contract as JSON"
              />
            </label>
            {contractSaveError && (
              <div className="imports-alert error" role="alert" data-testid="contract-save-error">
                {contractSaveError}
              </div>
            )}
            <div className="imports-editor-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => {
                  setEditingSheet(null);
                  setContractSaveError(null);
                }}
              >
                Cancel
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() => handleSaveContract(editingSheet === "__new__" ? "" : editingSheet)}
                disabled={contractSaving}
                data-testid="contract-save-btn"
              >
                {contractSaving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        )}
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Excel import                                                        */}
      {/* ------------------------------------------------------------------ */}
      <section className="imports-section">
        <h3 className="imports-section-title">Import an Excel file (.xlsx)</h3>
        <p className="imports-section-note">
          An import creates a <strong>candidate</strong> version — publish it explicitly to
          activate it. A failed import never touches the published version. Maximum size: 15 MB.
        </p>

        {fileError && (
          <div className="imports-alert error" role="alert" data-testid="file-error">
            {fileError}
          </div>
        )}
        {importError && (
          <div className="imports-alert error" role="alert" data-testid="import-error">
            {importError}
          </div>
        )}

        <div className="imports-uploader">
          <input
            ref={fileInputRef}
            type="file"
            accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            onChange={handleFileChange}
            disabled={importing}
            data-testid="file-input"
            aria-label="Select an Excel file to import"
            style={{ display: "none" }}
            id="xlsx-upload"
          />
          <label htmlFor="xlsx-upload">
            <span
              className="secondary-button"
              aria-disabled={importing}
              style={importing ? { opacity: 0.6, cursor: "default" } : { cursor: "pointer" }}
            >
              {importing ? "Importing…" : "Choose an .xlsx file"}
            </span>
          </label>
          {importing && <span className="imports-spinner" aria-hidden="true" />}
        </div>

        {/* ------------------------------------------------------------------ */}
        {/* Import report                                                       */}
        {/* ------------------------------------------------------------------ */}
        {importReport && (
          <div data-testid="import-report">
            <h3 className="imports-section-title">
              Import report — candidate version v{importReport.version.version_number}
            </h3>

            {/* Totals — the invariant, highlighted */}
            <div className="imports-sums" data-testid="import-sums">
              <div className="imports-sum">
                <span>File total</span>
                <strong data-testid="sum-file">
                  {fmtMoney(importReport.sums.file_total, plan.currency)}
                </strong>
              </div>
              <div className="imports-sum success">
                <span>Imported total</span>
                <strong data-testid="sum-imported">
                  {fmtMoney(importReport.sums.imported_total, plan.currency)}
                </strong>
              </div>
              <div className={`imports-sum${importReport.sums.rejected_total > 0 ? " error" : ""}`}>
                <span>Rejected total</span>
                <strong data-testid="sum-rejected">
                  {fmtMoney(importReport.sums.rejected_total, plan.currency)}
                </strong>
              </div>
            </div>

            {/* Per-line detail */}
            <div className="panel imports-panel" style={{ marginBottom: 16 }}>
              <div className="table-scroll" tabIndex={0} aria-label="Per-line import result">
                <table className="imports-table" data-testid="import-per-line-table">
                  <thead>
                    <tr>
                      <th>Line key</th>
                      <th>Result</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {importReport.per_line.map((row, i) => (
                      // eslint-disable-next-line react/no-array-index-key
                      <tr key={`${row.line_key}-${i}`} data-testid={`import-line-${row.line_key}`}>
                        <td className="imports-mono">{row.line_key}</td>
                        <td>
                          <span
                            className={`imports-chip ${STATUS_CHIP[row.status] ?? ""}`}
                            aria-label={`Status: ${row.status}`}
                          >
                            {row.status}
                          </span>
                        </td>
                        <td className="imports-cell-muted">{row.reason ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Explicit rejections, highlighted */}
            {importReport.rejected.length > 0 && (
              <div className="imports-alert warning" data-testid="import-rejets-alert">
                <span className="imports-alert-title">
                  {importReport.rejected.length} line(s) rejected:
                </span>
                <ul>
                  {importReport.rejected.map((r, i) => (
                    // eslint-disable-next-line react/no-array-index-key
                    <li key={`rej-${r.line_key}-${i}`}>
                      {r.line_key} — {r.reason ?? "unknown reason"}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Publish */}
            {!publishSuccess && (
              <>
                {publishError && (
                  <div className="imports-alert error" role="alert" data-testid="import-publish-error">
                    {publishError}
                  </div>
                )}
                <button
                  className="primary-button"
                  type="button"
                  onClick={handlePublishVersion}
                  disabled={publishingVersionId != null}
                  data-testid="import-publish-btn"
                >
                  {publishingVersionId ? "Publishing…" : "Publish version"}
                </button>
              </>
            )}
            {publishSuccess && (
              <div className="imports-alert success" role="status" data-testid="import-publish-success">
                Version published successfully — it is now the active version.
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
