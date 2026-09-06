/**
 * The entity x Datastream matrix (Story 48.5).
 *
 * The ratified Competitors contract calls for "a Project-wide matrix of entities
 * against compatible Datastreams" as the PRIMARY editor surface. That is a
 * genuinely two-dimensional shape, which the existing `CapabilityImpactMatrix`
 * is not -- it is one row per (capability, Datastream) pair. So this lives in
 * `ui/` beside it rather than as a `registry-matrix` class inside one screen.
 *
 * Three decisions worth reading before changing anything here.
 *
 * **A cell carries the binding state verbatim.** `published`, `candidate` and
 * `excluded` are three different facts, and a checkbox renders all three as a
 * tick. A candidate binding means an operator approved a Project intent; it does
 * not mean a single row was ever collected.
 *
 * **State is never colour alone.** Every cell shows a short glyph and a word,
 * and its accessible name states the entity, the Datastream and the state in a
 * sentence. Reduced motion, high contrast and monochrome printing all keep the
 * meaning.
 *
 * **The grid is not the only representation.** A wide matrix is unusable through
 * a screen reader and unreadable at 200% zoom, so the same data renders as a
 * per-entity list, switched by a real control rather than by a media query.
 */

import { useMemo, useRef, useState } from "react";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { NativeSelect } from "../components/ui/input";
import { EmptyState, TableScroll } from "./Data";
import type { Tone } from "./tone";

export type BindingState = "published" | "candidate" | "excluded" | "superseded" | "none";

export interface MatrixCell {
  datastream_id: string;
  state: BindingState;
  direction?: string | null;
  report_id?: string | null;
  exception_reason_code?: string | null;
  exception_reason?: string | null;
}

export interface MatrixEntity {
  entity_id: string;
  label: string;
  entity_kind?: string | null;
  role: string;
  cells: MatrixCell[];
  representation_count: number;
}

export interface MatrixDatastream {
  datastream_id: string;
  label: string;
}

const STATE_TONE: Record<BindingState, Tone> = {
  published: "success",
  candidate: "info",
  excluded: "neutral",
  superseded: "neutral",
  none: "neutral",
};

/** A word AND a glyph. Neither one alone carries the meaning. */
const STATE_LABEL: Record<BindingState, string> = {
  published: "Published",
  candidate: "Candidate",
  excluded: "Excluded",
  superseded: "Superseded",
  none: "Not bound",
};

const STATE_GLYPH: Record<BindingState, string> = {
  published: "●",
  candidate: "◐",
  excluded: "⊘",
  superseded: "○",
  none: "·",
};

/** The three readings of one binding state, for any surface that shows one.
 *
 *  Exported because the Competitor workbench's Coverage tab shows the SAME
 *  states as this matrix, one entity at a time, and a second `published →
 *  Published/green/●` map beside this one is how two screens start disagreeing
 *  about what a candidate binding looks like. An unknown state keeps its own
 *  word: a state the server invents must not be rendered as `Not bound`. */
export function bindingStateLabel(state: string): string {
  return STATE_LABEL[state as BindingState] ?? state;
}

export function bindingStateTone(state: string): Tone {
  return STATE_TONE[state as BindingState] ?? "neutral";
}

export function bindingStateGlyph(state: string): string {
  return STATE_GLYPH[state as BindingState] ?? "·";
}

const ROLE_LABEL: Record<string, string> = {
  own: "Own",
  competitor: "Competitor",
  reference: "Reference",
};

function roleLabel(role: string): string {
  return ROLE_LABEL[role] ?? role;
}

function cellFor(entity: MatrixEntity, datastreamId: string): MatrixCell {
  return (
    entity.cells.find((cell) => cell.datastream_id === datastreamId) ?? {
      datastream_id: datastreamId,
      state: "none",
    }
  );
}

function describe(entity: MatrixEntity, datastream: MatrixDatastream, cell: MatrixCell): string {
  const base = `${entity.label}, ${datastream.label}: ${STATE_LABEL[cell.state]}`;
  if (cell.state === "excluded" && cell.exception_reason) return `${base}. ${cell.exception_reason}`;
  return base;
}

export function EntityMatrix({
  entities,
  datastreams,
  emptyMessage = "No tracked entity is associated with this Project yet.",
}: {
  entities: MatrixEntity[];
  datastreams: MatrixDatastream[];
  emptyMessage?: string;
}) {
  const [roleFilter, setRoleFilter] = useState("all");
  const [stateFilter, setStateFilter] = useState("all");
  const [datastreamFilter, setDatastreamFilter] = useState("all");
  const [asList, setAsList] = useState(false);
  const [focused, setFocused] = useState<{ row: number; column: number } | null>(null);
  const gridRef = useRef<HTMLTableElement | null>(null);

  const visibleDatastreams = useMemo(
    () =>
      datastreamFilter === "all"
        ? datastreams
        : datastreams.filter((item) => item.datastream_id === datastreamFilter),
    [datastreams, datastreamFilter],
  );

  const visibleEntities = useMemo(
    () =>
      entities.filter((entity) => {
        if (roleFilter !== "all" && entity.role !== roleFilter) return false;
        if (stateFilter === "all") return true;
        return visibleDatastreams.some(
          (datastream) => cellFor(entity, datastream.datastream_id).state === stateFilter,
        );
      }),
    [entities, roleFilter, stateFilter, visibleDatastreams],
  );

  const roles = useMemo(
    () => Array.from(new Set(entities.map((entity) => entity.role))).sort(),
    [entities],
  );

  const focusCell = (row: number, column: number) => {
    const clampedRow = Math.max(0, Math.min(row, visibleEntities.length - 1));
    const clampedColumn = Math.max(0, Math.min(column, visibleDatastreams.length - 1));
    setFocused({ row: clampedRow, column: clampedColumn });
    const selector = `[data-cell="${clampedRow}:${clampedColumn}"]`;
    const target = gridRef.current?.querySelector<HTMLElement>(selector);
    target?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent, row: number, column: number) => {
    const moves: Record<string, [number, number]> = {
      ArrowUp: [row - 1, column],
      ArrowDown: [row + 1, column],
      ArrowLeft: [row, column - 1],
      ArrowRight: [row, column + 1],
      Home: [row, 0],
      End: [row, visibleDatastreams.length - 1],
    };
    const next = moves[event.key];
    if (!next) return;
    event.preventDefault();
    focusCell(next[0], next[1]);
  };

  if (entities.length === 0) {
    return <p className="m-0 text-ui text-text-secondary">{emptyMessage}</p>;
  }

  const focusedEntity = focused ? visibleEntities[focused.row] : undefined;
  const focusedDatastream = focused ? visibleDatastreams[focused.column] : undefined;
  const focusedCell =
    focusedEntity && focusedDatastream
      ? cellFor(focusedEntity, focusedDatastream.datastream_id)
      : undefined;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-caption text-text-secondary">
          Role
          <NativeSelect
            value={roleFilter}
            onChange={(event) => setRoleFilter(event.target.value)}
            aria-label="Filter by Project role"
          >
            <option value="all">All roles</option>
            {roles.map((role) => (
              <option key={role} value={role}>
                {roleLabel(role)}
              </option>
            ))}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1 text-caption text-text-secondary">
          Binding
          <NativeSelect
            value={stateFilter}
            onChange={(event) => setStateFilter(event.target.value)}
            aria-label="Filter by binding state"
          >
            <option value="all">Any state</option>
            {(["published", "candidate", "excluded", "none"] as BindingState[]).map((state) => (
              <option key={state} value={state}>
                {STATE_LABEL[state]}
              </option>
            ))}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1 text-caption text-text-secondary">
          Datastream
          <NativeSelect
            value={datastreamFilter}
            onChange={(event) => setDatastreamFilter(event.target.value)}
            aria-label="Filter by Datastream"
          >
            <option value="all">All Datastreams</option>
            {datastreams.map((item) => (
              <option key={item.datastream_id} value={item.datastream_id}>
                {item.label}
              </option>
            ))}
          </NativeSelect>
        </label>
        <Button variant="secondary" onClick={() => setAsList((value) => !value)}>
          {asList ? "Show as grid" : "Show as list"}
        </Button>
      </div>

      {visibleEntities.length === 0 ? (
        <EmptyState
          title="No tracked entity matches these filters"
          description="Nothing has been hidden beyond them. Widen the role or the search above to see the rest of this Project's registry."
        />
      ) : asList || visibleDatastreams.length === 0 ? (
        <>
        {/* A MATRIX-WIDE FACT, SAID ONCE (76-4, second review). This was drawn
            inside the per-entity `<li>`, so a Project with twelve tracked
            entities and no compatible Datastream stacked twelve identical
            blocks -- against the container rule the same story ratified:
            `EmptyState` answers for a REGION. The entities below still list,
            because they exist; what is absent is the axis they would be crossed
            with, and that is one absence. */}
        {visibleDatastreams.length === 0 && (
          <EmptyState
            title="No compatible Datastream"
            description="No Datastream of this Project carries the fields these entities would be matched on, so no cell can be drawn. Each entity is listed below with its role, and nothing is claimed about its coverage."
          />
        )}
        <ul className="m-0 flex list-none flex-col gap-4 p-0" data-testid="entity-matrix-list">
          {visibleEntities.map((entity) => (
            <li key={entity.entity_id} className="flex flex-col gap-2">
              <p className="m-0 text-ui font-semibold text-text">
                {entity.label}{" "}
                <Badge tone="neutral">{roleLabel(entity.role)}</Badge>
              </p>
              {visibleDatastreams.length === 0 ? null : (
                <ul className="m-0 flex list-none flex-col gap-1 p-0">
                  {visibleDatastreams.map((datastream) => {
                    const cell = cellFor(entity, datastream.datastream_id);
                    return (
                      <li
                        key={datastream.datastream_id}
                        className="text-caption text-text-secondary"
                      >
                        {datastream.label} — {STATE_LABEL[cell.state]}
                        {cell.exception_reason ? ` — ${cell.exception_reason}` : ""}
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          ))}
        </ul>
        </>
      ) : (
        <TableScroll label="Tracked entities by Datastream">
          <table
            ref={gridRef}
            className="w-full border-collapse text-ui"
            data-testid="entity-matrix-grid"
          >
            <caption className="sr-only">
              Tracked entities by Datastream. Use the arrow keys to move between cells.
            </caption>
            <thead>
              <tr>
                <th scope="col" className="p-2 text-left text-caption text-text-secondary">
                  Entity
                </th>
                <th scope="col" className="p-2 text-left text-caption text-text-secondary">
                  Role
                </th>
                {visibleDatastreams.map((datastream) => (
                  <th
                    key={datastream.datastream_id}
                    scope="col"
                    className="p-2 text-left text-caption text-text-secondary"
                  >
                    {datastream.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleEntities.map((entity, row) => (
                <tr key={entity.entity_id}>
                  <th scope="row" className="p-2 text-left font-semibold text-text">
                    {entity.label}
                  </th>
                  <td className="p-2 text-text-secondary">{roleLabel(entity.role)}</td>
                  {visibleDatastreams.map((datastream, column) => {
                    const cell = cellFor(entity, datastream.datastream_id);
                    const isFocused = focused?.row === row && focused?.column === column;
                    const isFirst = focused === null && row === 0 && column === 0;
                    return (
                      <td key={datastream.datastream_id} className="p-1">
                        <button
                          type="button"
                          data-cell={`${row}:${column}`}
                          tabIndex={isFocused || isFirst ? 0 : -1}
                          aria-label={describe(entity, datastream, cell)}
                          className="flex w-full items-center gap-2 rounded px-2 py-1 text-left focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                          onFocus={() => setFocused({ row, column })}
                          onKeyDown={(event) => onKeyDown(event, row, column)}
                        >
                          <span aria-hidden="true">{STATE_GLYPH[cell.state]}</span>
                          <Badge tone={STATE_TONE[cell.state]}>{STATE_LABEL[cell.state]}</Badge>
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}

      {focusedCell && focusedEntity && focusedDatastream && (
        <div
          className="rounded border border-border p-3"
          role="status"
          data-testid="entity-matrix-detail"
        >
          <p className="m-0 text-ui font-semibold text-text">
            {focusedEntity.label} — {focusedDatastream.label}
          </p>
          <p className="m-0 text-caption text-text-secondary">
            {STATE_LABEL[focusedCell.state]}
            {focusedCell.report_id ? ` · report ${focusedCell.report_id}` : ""}
            {focusedCell.direction ? ` · ${focusedCell.direction}` : ""}
          </p>
          {focusedCell.exception_reason && (
            <p className="m-0 text-caption text-text-secondary">{focusedCell.exception_reason}</p>
          )}
          <p className="m-0 text-caption text-text-secondary">
            {focusedEntity.representation_count} governed source representation
            {focusedEntity.representation_count === 1 ? "" : "s"}. Identity alignment does not
            authorize metric comparison.
          </p>
        </div>
      )}
    </div>
  );
}
