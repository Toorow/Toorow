/**
 * The left rail: the governed field catalog (AC7).
 *
 * FOUR GROUPS, ALWAYS. `visualization-and-rendering.md:140-142` names Metrics,
 * Dimensions, Time and Classifications, and all four are rendered. Two of them
 * are rendered EMPTY WITH THEIR REASON, because the compiled Semantic View
 * carries no time or classification role today.
 *
 * Hiding those two groups would erase the inventory of remaining work: a reader
 * would see a complete-looking rail and never learn that two thirds of the
 * vocabulary is waiting on another surface. Guessing their contents from member
 * names would be worse -- it would manufacture a semantic authority the Builder
 * is forbidden to have, and the guess would be invisible in the saved spec.
 *
 * THE RAIL LISTS EXACTLY WHAT THE QUERY SELECTED. Not the Semantic View's full
 * member set. Adding a member is a query change, and the rail says so in one
 * sentence rather than offering a member the Query Spec never asked for -- a
 * browser-side catalog is a second semantic authority.
 */
import { DraggableField, EmptyState, FieldShelf, Panel, PanelHeader, Status } from "../../ui";
import type {
  MemberFacet,
  VisualizationMember,
  VisualizationOptions,
} from "../visualizationClient";

/**
 * AC7's five facets, rendered per rail entry — each either the value the server
 * READ, or an explicit absence naming the surface that owns writing it.
 *
 * The order and the membership of this list are the SERVER's
 * (`member_metadata_facets`), not a copy typed here: a facet the server stops
 * serving stops being rendered, and a sixth one reaches the rail without a client
 * change. Only the human label is local, and only because a rail heading is
 * presentation.
 *
 * Rendering a missing definition as a blank cell was the earlier behaviour, and
 * it is the same failure the `Time` and `Classifications` groups are built to
 * avoid: an absence with no owner reads as "nothing to see here" rather than as
 * "another surface has not written this yet".
 */
const FACET_LABELS: Record<string, string> = {
  definition: "Definition",
  grain: "Grain",
  additivity: "Additivity",
  quality_state: "Quality state",
  provenance_hint: "Provenance",
};

const FALLBACK_FACETS = [
  "definition",
  "grain",
  "additivity",
  "quality_state",
  "provenance_hint",
];

function facetLabel(facet: string): string {
  return FACET_LABELS[facet] ?? facet.replace(/_/g, " ");
}

export function MemberMetadata({
  member,
  facets,
}: {
  member: VisualizationMember;
  facets: string[];
}) {
  const metadata = member.metadata ?? {};
  return (
    <dl className="m-0 mt-1 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-caption">
      {facets.map((facet) => {
        const entry: MemberFacet | undefined = metadata[facet];
        const value = entry?.value ?? null;
        return (
          <div key={facet} className="contents">
            <dt className="text-text-secondary">{facetLabel(facet)}</dt>
            <dd className="m-0 text-text" data-testid={`member-${member.id}-${facet}`}>
              {value ?? (
                <span className="text-text-secondary">
                  Not recorded — {entry?.owner ?? "no surface is named for this field"} owns it.
                </span>
              )}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

interface Group {
  key: string;
  label: string;
  members: VisualizationMember[];
  reason: string | null;
  owner: string | null;
}

export function catalogGroups(options: VisualizationOptions): Group[] {
  const roles = new Map(options.roles.map((r) => [r.role, r]));
  const unavailable = (role: string) => {
    const entry = roles.get(role);
    return entry && !entry.available
      ? { reason: entry.reason ?? options.unavailable_reason, owner: entry.owner ?? options.unavailable_owner }
      : { reason: null, owner: null };
  };
  return [
    { key: "metrics", label: "Metrics", members: options.measures, reason: null, owner: null },
    { key: "dimensions", label: "Dimensions", members: options.dimensions, reason: null, owner: null },
    { key: "time", label: "Time", members: options.time, ...unavailable("time") },
    {
      key: "classifications",
      label: "Classifications",
      members: options.classifications,
      ...unavailable("classification"),
    },
  ];
}

export default function FieldCatalogRail({
  options,
  exploreHref,
}: {
  options: VisualizationOptions;
  /** Null when the Explore workbench is not mounted; the rail then says so
   *  plainly rather than linking to a route that 404s. */
  exploreHref: string | null;
}) {
  // The server's list, with a local fallback only so an older response still
  // renders the five facets AC7 names rather than none.
  const facets = options.member_metadata_facets?.length
    ? options.member_metadata_facets
    : FALLBACK_FACETS;
  return (
    <Panel flush aria-labelledby="field-catalog-heading">
      <PanelHeader
        title="Field catalog"
        description="Exactly the members this query selected."
      />
      {/* The rail is also where a bound member is DROPPED to unbind it: the
          place a member is listed when unbound takes it back, with the same
          consequence as its remove button on the well. */}
      <FieldShelf testId="catalog-rail-drop" className="flex flex-col gap-5 p-5">
        <p id="field-catalog-heading" className="m-0 text-caption text-text-secondary">
          Adding a member is a query change: it produces a new Result.{" "}
          {exploreHref ? (
            <a className="underline" href={exploreHref}>
              Open Explore to change the query.
            </a>
          ) : (
            <span>Explore is not mounted in this build, so the query cannot be changed here.</span>
          )}
        </p>

        {catalogGroups(options).map((group) => (
          <section key={group.key} aria-labelledby={`catalog-${group.key}`}>
            <h3
              id={`catalog-${group.key}`}
              className="m-0 mb-2 text-label font-semibold text-text-secondary"
            >
              {group.label}
            </h3>

            {group.reason ? (
              <Status
                as="block"
                tone="warning"
                title={`${group.label} is unavailable`}
                data-testid={`catalog-${group.key}-unavailable`}
              >
                {group.reason} Owner: {group.owner}.
              </Status>
            ) : group.members.length === 0 ? (
              <EmptyState
                title={`No ${group.label.toLowerCase()} in this query`}
                description="This Query Spec version selected none. Add one in Explore to make it bindable."
              />
            ) : (
              <ul className="m-0 flex list-none flex-col gap-1 p-0">
                {group.members.map((member) => (
                  <li key={member.id} data-testid={`catalog-member-${member.id}`}>
                    {/* THE RAIL IS WHERE A MEMBER IS PICKED UP. It carries the
                        five facets as it always did -- a catalog whose entries
                        cannot be moved is a list, and the wells beside it would
                        be a second place to say the same thing. */}
                    <DraggableField field={{
                      id: member.id,
                      label: member.label,
                      role: member.role,
                    }} className="w-full">
                      <span className="block text-ui text-text">{member.label}</span>
                      <span className="block text-caption text-text-secondary">
                        {member.role} · pinned version {member.version_id || "not recorded"}
                      </span>
                      <MemberMetadata member={member} facets={facets} />
                    </DraggableField>
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}

        <section aria-labelledby="catalog-query-facts">
          <h3
            id="catalog-query-facts"
            className="m-0 mb-2 text-label font-semibold text-text-secondary"
          >
            What the query pinned
          </h3>
          <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-1 text-caption">
            <dt className="text-text-secondary">Time grain</dt>
            <dd className="m-0 text-text">{options.grain ?? "none recorded"}</dd>
            <dt className="text-text-secondary">Comparison</dt>
            <dd className="m-0 text-text">{options.comparison}</dd>
            <dt className="text-text-secondary">Row limit</dt>
            <dd className="m-0 text-text">{options.row_limit ?? "not recorded"}</dd>
          </dl>
        </section>
      </FieldShelf>
    </Panel>
  );
}
