/**
 * DataModelPage — the semantic / metric model surface (Governance ▸ Semantic
 * model). A data dictionary where canonical fields are UNIFIED (N source fields
 * → one canonical metric/dimension with measure semantics) and ATTRIBUTED (which
 * datastreams feed each field). "Which flows feed clicks?" is answered in one
 * click.
 *
 * v3 restyle: renders inside the application shell's <main>. Chrome uses the
 * global application.css classes (page-header, header-actions, panel,
 * primary-button, selector) plus the page-specific semantic-model.css. Colors
 * come exclusively from CSS variables so the surface is dark-safe; the field
 * table lives in FieldsTable, the detail drawer in FieldDetailDrawer, and field
 * creation in NewFieldDialog.
 *
 * Behavior is unchanged from the original:
 *  - Module options derived from GET /api/datastreams (unique module_name).
 *  - GET /api/datamodel/fields?kind=&usage=&module= drives the table.
 *  - Selecting a module re-fetches with ?module=<name>; "All modules" resets.
 *  - Clicking a field opens the detail drawer; the "+ New field" button opens
 *    the creation dialog.
 */

import {
  FormControl,
  MenuItem,
  Select,
  SelectChangeEvent,
} from "@mui/material";
import { useEffect, useMemo, useState } from "react";
import FieldDetailDrawer, { AvailableField } from "./datamodel/FieldDetailDrawer";
import FieldsTable, { TargetField } from "./datamodel/FieldsTable";
import NewFieldDialog from "./datamodel/NewFieldDialog";
import "./shell/application.css";
import "./semantic-model.css";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface DataModelPageProps {
  projectId?: string;
}

// ---------------------------------------------------------------------------
// Filter types
// ---------------------------------------------------------------------------

type KindFilter = "all" | "metric" | "dimension";
type UsageFilter = "all" | "used" | "unmapped";

// ---------------------------------------------------------------------------
// Shared MUI Select style: token-driven, dark-safe. Kept as MUI Select because
// the module-options portal behavior is part of the tested contract.
// ---------------------------------------------------------------------------

const selectSx = {
  minHeight: 40,
  borderRadius: "999px",
  fontSize: "13px",
  fontWeight: 700,
  color: "var(--ink)",
  backgroundColor: "var(--surface)",
  "& .MuiOutlinedInput-notchedOutline": { borderColor: "var(--line)" },
  "&:hover .MuiOutlinedInput-notchedOutline": { borderColor: "var(--line)" },
  "&.Mui-focused .MuiOutlinedInput-notchedOutline": { borderColor: "var(--rose)" },
  "& .MuiSelect-select": { py: "8px" },
} as const;

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function DataModelPage({ projectId }: DataModelPageProps) {
  // ---- State ---------------------------------------------------------------
  const [fields, setFields] = useState<TargetField[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const [usageFilter, setUsageFilter] = useState<UsageFilter>("all");
  const [moduleFilter, setModuleFilter] = useState<string>("all");

  // Module options — derived from GET /api/datastreams (unique module_name values).
  const [moduleOptions, setModuleOptions] = useState<string[]>([]);

  const [selectedField, setSelectedField] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [newFieldOpen, setNewFieldOpen] = useState(false);

  // ---- Fetch module options from /api/datastreams --------------------------
  useEffect(() => {
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    fetch(`/api/datastreams?${params.toString()}`, {
      headers: {
        Authorization: `Bearer ${localStorage.getItem("api_token") || ""}`,
      },
    })
      .then((r) => {
        if (!r.ok) return;
        return r.json();
      })
      .then((data: Array<{ module_name?: string }> | undefined) => {
        if (!Array.isArray(data)) return;
        const unique = Array.from(
          new Set(
            data
              .map((d) => d.module_name)
              .filter((m): m is string => typeof m === "string" && m.length > 0),
          ),
        ).sort();
        setModuleOptions(unique);
      })
      .catch(() => {
        // Non-fatal: module filter stays empty; "All modules" default still works.
      });
  }, [projectId]);

  // ---- Fetch fields --------------------------------------------------------
  const fetchFields = () => {
    setLoading(true);
    setLoadError(null);

    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    if (kindFilter !== "all") params.set("kind", kindFilter);
    if (usageFilter !== "all") params.set("usage", usageFilter);
    if (moduleFilter !== "all") params.set("module", moduleFilter);

    const url = `/api/datamodel/fields?${params.toString()}`;
    fetch(url, {
      headers: {
        Authorization: `Bearer ${localStorage.getItem("api_token") || ""}`,
      },
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: TargetField[]) => {
        setFields(data);
      })
      .catch((e) => {
        setLoadError(String(e));
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchFields();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, kindFilter, usageFilter, moduleFilter]);

  // ---- Derived counts for the summary strip -------------------------------
  const metrics = useMemo(
    () => fields.filter((f) => f.field_kind === "metric"),
    [fields],
  );
  const dimensions = useMemo(
    () => fields.filter((f) => f.field_kind === "dimension"),
    [fields],
  );
  const unmapped = useMemo(
    () => fields.filter((f) => f.used_by_count === 0),
    [fields],
  );

  // ---- Available target fields for the remap dropdown ----------------------
  const availableFields: AvailableField[] = useMemo(
    () =>
      fields.map((f) => ({ name: f.name, display_name: f.display_name })),
    [fields],
  );

  // ---- displayedFields — server already filters by ?module=; expose as-is --
  const displayedFields = fields;

  // ---- Handlers ------------------------------------------------------------
  const handleFieldClick = (name: string) => {
    setSelectedField(name);
    setDrawerOpen(true);
  };

  const handleDrawerClose = () => {
    setDrawerOpen(false);
  };

  const handleFieldCreated = () => {
    fetchFields();
  };

  // ---- Render --------------------------------------------------------------
  return (
    <>
      {/* ---- Page header --------------------------------------------------- */}
      <div className="page-header">
        <div>
          <h1>Semantic model</h1>
          <p>
            The data dictionary — canonical fields, unified and attributed to the
            datastreams that feed them. Select a field to see which flows supply it.
          </p>
        </div>
        <div className="header-actions">
          <button
            className="primary-button"
            type="button"
            onClick={() => setNewFieldOpen(true)}
            data-testid="new-field-button"
          >
            + New field
          </button>
        </div>
      </div>

      {/* ---- Summary strip ------------------------------------------------- */}
      {!loading && fields.length > 0 && (
        <section className="sm-summary" data-testid="kpi-strip">
          <div>
            <span>Metrics</span>
            <strong data-testid="count-metrics">{metrics.length}</strong>
            <small>Measurable values</small>
          </div>
          <div>
            <span>Dimensions</span>
            <strong data-testid="count-dimensions">{dimensions.length}</strong>
            <small>Breakdown attributes</small>
          </div>
          <div>
            <span>Unmapped</span>
            <strong data-testid="count-unmapped">{unmapped.length}</strong>
            <small>No datastream source configured</small>
          </div>
        </section>
      )}

      {/* ---- Fields panel -------------------------------------------------- */}
      <section className="panel sm-panel">
        <div className="sm-toolbar" data-testid="filter-bar">
          <div className="sm-filters">
            {/* Kind filter */}
            <FormControl size="small" sx={{ minWidth: 140 }}>
              <Select
                value={kindFilter}
                onChange={(e: SelectChangeEvent) =>
                  setKindFilter(e.target.value as KindFilter)
                }
                displayEmpty
                inputProps={{ "data-testid": "filter-kind" }}
                sx={selectSx}
              >
                <MenuItem value="all">All types</MenuItem>
                <MenuItem value="metric">Metrics</MenuItem>
                <MenuItem value="dimension">Dimensions</MenuItem>
              </Select>
            </FormControl>

            {/* Usage filter */}
            <FormControl size="small" sx={{ minWidth: 150 }}>
              <Select
                value={usageFilter}
                onChange={(e: SelectChangeEvent) =>
                  setUsageFilter(e.target.value as UsageFilter)
                }
                displayEmpty
                inputProps={{ "data-testid": "filter-usage" }}
                sx={selectSx}
              >
                <MenuItem value="all">All fields</MenuItem>
                <MenuItem value="used">Used</MenuItem>
                <MenuItem value="unmapped">Unmapped</MenuItem>
              </Select>
            </FormControl>

            {/* Module filter */}
            <FormControl size="small" sx={{ minWidth: 160 }}>
              <Select
                value={moduleFilter}
                onChange={(e: SelectChangeEvent) => {
                  setModuleFilter(e.target.value);
                }}
                displayEmpty
                inputProps={{ "data-testid": "filter-module" }}
                sx={selectSx}
              >
                <MenuItem value="all">All modules</MenuItem>
                {moduleOptions.map((mod) => (
                  <MenuItem key={mod} value={mod}>
                    {mod}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </div>

          {/* Results count hint */}
          {!loading && (
            <span className="sm-count">
              {displayedFields.length}{" "}
              {displayedFields.length === 1 ? "field" : "fields"}
            </span>
          )}
        </div>

        {/* ---- Table / states --------------------------------------------- */}
        {loading && (
          <div className="sm-state" data-testid="fields-loading">
            <p>Loading fields…</p>
          </div>
        )}
        {!loading && loadError && (
          <div className="sm-state error">
            <p>Could not load fields: {loadError}</p>
            <button
              type="button"
              className="sm-retry"
              onClick={fetchFields}
            >
              Retry
            </button>
          </div>
        )}
        {!loading && !loadError && (
          <FieldsTable
            fields={displayedFields}
            onFieldClick={handleFieldClick}
            selectedField={selectedField}
            filterHint={moduleFilter !== "all" ? moduleFilter : undefined}
          />
        )}
      </section>

      {/* ---- Detail drawer ------------------------------------------------- */}
      <FieldDetailDrawer
        fieldName={selectedField}
        open={drawerOpen}
        onClose={handleDrawerClose}
        availableFields={availableFields}
        onMappingChanged={fetchFields}
        onFieldChanged={fetchFields}
      />

      {/* ---- New field dialog ---------------------------------------------- */}
      <NewFieldDialog
        open={newFieldOpen}
        onClose={() => setNewFieldOpen(false)}
        onCreated={handleFieldCreated}
      />
    </>
  );
}
