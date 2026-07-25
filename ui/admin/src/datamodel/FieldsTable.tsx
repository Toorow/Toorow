/**
 * FieldsTable — semantic-model fields table for DataModelPage.
 *
 * v3 restyle: plain HTML table using the semantic-model.css classes (mirrors the
 * governance concept table). The field name is primary, the used-by count is the
 * number hero (Geist tabular), kind is a tinted-dot pill, and attribution /
 * approval state render as tokenized status chips. All colors come from
 * application.css CSS variables so the surface flips in dark mode.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TargetField {
  name: string;
  display_name: string;
  data_type: string;
  field_kind: "metric" | "dimension";
  measure: string | null;
  description: string | null;
  created_by: string;
  is_default: boolean;
  created_at: string;
  used_by_count: number;
  /** Story 13.1: 'draft' | 'approved' */
  status?: "draft" | "approved";
}

interface FieldsTableProps {
  fields: TargetField[];
  onFieldClick: (name: string) => void;
  selectedField?: string | null;
  /** When set, shown as a secondary hint in the empty state (e.g. active filter name). */
  filterHint?: string;
}

// ---------------------------------------------------------------------------
// Kind indicator: tinted dot + label
// ---------------------------------------------------------------------------

function KindIndicator({ kind }: { kind: "metric" | "dimension" }) {
  return (
    <span className={`sm-kind ${kind}`}>
      <span className="sm-kind-dot" />
      <strong>{kind === "metric" ? "Metric" : "Dimension"}</strong>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Status chip: draft (approval pending) takes priority, else unmapped
// ---------------------------------------------------------------------------

function StatusChip({
  count,
  status,
}: {
  count: number;
  status?: "draft" | "approved";
}) {
  if (status === "draft") {
    return (
      <span
        className="sm-status-chip draft"
        title="Pending approval — this field does not feed reports yet"
      >
        <span className="sm-status-dot" />
        Draft
      </span>
    );
  }
  if (count === 0) {
    return (
      <span
        className="sm-status-chip unmapped"
        title="Unmapped — no flow feeds this field"
      >
        <span className="sm-status-dot" />
        Unmapped
      </span>
    );
  }
  return null;
}

// ---------------------------------------------------------------------------
// Main table
// ---------------------------------------------------------------------------

export default function FieldsTable({
  fields,
  onFieldClick,
  selectedField,
  filterHint,
}: FieldsTableProps) {
  if (fields.length === 0) {
    return (
      <div className="sm-state" data-testid="fields-empty-state">
        <p>No field matches the selected filters.</p>
        {filterHint && (
          <p className="sm-state-hint" data-testid="fields-empty-filter-hint">
            Active filter: module “{filterHint}”
          </p>
        )}
      </div>
    );
  }

  return (
    <div
      className="sm-table-scroll"
      tabIndex={0}
      aria-label="Scrollable semantic model fields table"
    >
      <table className="sm-table" data-testid="fields-table">
        <thead>
          <tr>
            <th>Field</th>
            <th>Type</th>
            <th>Measure</th>
            <th className="sm-align-right">Fed by</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((field) => {
            const isSelected = selectedField === field.name;
            return (
              <tr
                key={field.name}
                className={`sm-row${isSelected ? " selected" : ""}`}
                onClick={() => onFieldClick(field.name)}
                data-testid={`field-row-${field.name}`}
              >
                {/* Field */}
                <td>
                  <div className="sm-field">
                    <strong>{field.display_name}</strong>
                    <small>{field.name}</small>
                  </div>
                </td>

                {/* Type (kind + data_type) */}
                <td>
                  <KindIndicator kind={field.field_kind} />
                  <span className="sm-datatype">{field.data_type}</span>
                </td>

                {/* Measure */}
                <td>
                  {field.measure ? (
                    <span className="sm-measure">{field.measure}</span>
                  ) : (
                    <span className="sm-muted">—</span>
                  )}
                </td>

                {/* Fed by N flows — number is the hero */}
                <td className="sm-usedby">
                  {field.used_by_count > 0 ? (
                    <span>
                      <span className="sm-usedby-value">
                        {field.used_by_count}
                      </span>
                      <span className="sm-usedby-unit">
                        {field.used_by_count === 1 ? "flow" : "flows"}
                      </span>
                    </span>
                  ) : (
                    <span className="sm-usedby-zero">0</span>
                  )}
                </td>

                {/* Status */}
                <td>
                  <StatusChip count={field.used_by_count} status={field.status} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
