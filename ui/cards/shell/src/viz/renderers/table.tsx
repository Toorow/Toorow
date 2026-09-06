/**
 * Story 50.5 -- the `table` family.
 *
 * The one family whose visual IS the fallback. It has no chart-side volume limit
 * (`compile/limits.ts`): it is bounded only by the Result slice the server
 * returned, so a large answer is shown in full rather than replaced by a smaller
 * chart of itself.
 */

import TableFallback from "./tableFallback";
import type { RendererProps } from "../registry";

export default function TableRenderer(props: RendererProps) {
  const { model, input, display, onFeedbackTarget, datumRowOffset } = props;
  const targetableFields = [...new Set(Object.values(model.datumTargets).map((target) => target.field))];
  return (
    <div data-viz-family="table" data-viz-chart-drawn="true">
      <TableFallback
        columns={model.tableColumns}
        rows={input.result.rows}
        caption="Every row returned by this query"
        numberStyle={model.formats.numberStyle}
        dateStyle={model.formats.dateStyle}
        unit={model.formats.unit}
        hiddenRowKeys={display.localHiddenRows}
        targetableFields={targetableFields}
        onFeedbackTarget={onFeedbackTarget ? (row, field) => onFeedbackTarget(
          { kind: "datum", row_index: datumRowOffset + row, field },
          `Row ${datumRowOffset + row + 1} · ${field}`,
        ) : undefined}
      />
    </div>
  );
}
