/**
 * Story 50.5 AC5 -- the ONE specialized family of this release: the evidence path.
 *
 * D3 CALCULATES; REACT RENDERS. `d3-hierarchy` computes the tree layout and
 * `d3-shape` computes the link geometry. Neither touches the document: the
 * selection package is not installed, no DOM node is created outside React's
 * tree, and no element attribute is mutated imperatively. The guard test asserts
 * that by grep, because the boundary is easy to state and easy to lose.
 *
 * WHY NOT ECharts' `tree` SERIES. It would draw the nodes on a canvas, where an
 * evidence node cannot be a focusable React element that opens the evidence
 * drawer and restores focus to its invoker (AC13), and it would make the
 * specialized family indistinguishable from a standard one in the registry --
 * which is exactly the boundary `visualization-and-rendering.md:204-208` draws.
 *
 * IT IS NOT A REGISTERED SPEC FAMILY, and that is a measured limitation, not an
 * oversight. Story 50.4's family enum is closed at seven ids
 * (`server/core/visualization_families.py:386`) and carries no id for an evidence
 * path -- `ai_path` is in its DEFERRED list. So a Visualization Spec cannot ask
 * for this family today. It is mounted by the evidence drawer, where the data it
 * draws (the Result's own provenance chain) actually exists. Registering it under
 * an invented eighth family id would have added a family the server refuses.
 */

import { hierarchy, tree, type HierarchyPointNode } from "d3-hierarchy";
import { linkHorizontal } from "d3-shape";

import type { DatumEvidence } from "../evidence/resolve";

interface EvidenceNode {
  id: string;
  label: string;
  detail: string | null;
  children?: EvidenceNode[];
}

const NODE_WIDTH = 190;
const NODE_HEIGHT = 34;
const LEVEL_GAP = 210;

/**
 * Build the evidence hierarchy from ONE resolved datum. Every node is a fact the
 * Result already carries; nothing is derived and nothing is looked up.
 */
export function evidenceHierarchy(evidence: DatumEvidence): EvidenceNode {
  const fieldNodes: EvidenceNode[] = evidence.fields.map((field) => {
    const provenance = evidence.provenance.find((p) => p.member_id === field);
    const children: EvidenceNode[] = [];
    if (provenance?.source_system) {
      children.push({
        id: `${field}/source`,
        label: provenance.source_system,
        detail: provenance.source_field ? `field ${provenance.source_field}` : null,
      });
    }
    if (provenance?.pull_id) {
      children.push({ id: `${field}/pull`, label: "Pull", detail: provenance.pull_id });
    }
    return {
      id: `field/${field}`,
      label: field,
      detail:
        field in evidence.values ? `value ${String(evidence.values[field] ?? "--")}` : null,
      children: children.length > 0 ? children : undefined,
    };
  });

  const roots: EvidenceNode[] = [...fieldNodes];
  if (evidence.mappingVersionId) {
    roots.push({
      id: "mapping",
      label: "Mapping version",
      detail: evidence.mappingVersionId,
    });
  }
  if (evidence.publicationLogId) {
    roots.push({
      id: "publication",
      label: "Publication",
      detail: evidence.publicationLogId,
    });
  }

  return {
    id: "result",
    label: "Result",
    detail: evidence.contentHash,
    children: roots,
  };
}

export interface EvidencePathProps {
  evidence: DatumEvidence;
  onNodeActivate?: (nodeId: string) => void;
}

export default function EvidencePath(props: EvidencePathProps) {
  const { evidence, onNodeActivate } = props;
  const root = hierarchy<EvidenceNode>(evidenceHierarchy(evidence));
  const layout = tree<EvidenceNode>().nodeSize([NODE_HEIGHT + 14, LEVEL_GAP]);
  const positioned = layout(root);
  const nodes = positioned.descendants();
  const links = positioned.links();

  let minX = 0;
  let maxX = 0;
  let maxY = 0;
  for (const node of nodes) {
    if (node.x < minX) minX = node.x;
    if (node.x > maxX) maxX = node.x;
    if (node.y > maxY) maxY = node.y;
  }
  const height = maxX - minX + NODE_HEIGHT * 2;
  const width = maxY + NODE_WIDTH + 24;

  // Geometry only. `linkHorizontal` returns a path STRING; React writes it.
  const link = linkHorizontal<
    { source: HierarchyPointNode<EvidenceNode>; target: HierarchyPointNode<EvidenceNode> },
    HierarchyPointNode<EvidenceNode>
  >()
    .x((d) => d.y)
    .y((d) => d.x - minX + NODE_HEIGHT);

  return (
    <svg
      role="tree"
      aria-label="Evidence path for this mark"
      viewBox={`0 0 ${width} ${height}`}
      width="100%"
      height={height}
      data-viz-evidence-path="true"
    >
      <g fill="none" stroke="currentColor" strokeOpacity={0.35}>
        {links.map((edge) => (
          <path key={`${edge.source.data.id}->${edge.target.data.id}`} d={link(edge) ?? undefined} />
        ))}
      </g>
      {nodes.map((node) => (
        <g
          key={node.data.id}
          role="treeitem"
          aria-level={node.depth + 1}
          aria-label={`${node.data.label}${node.data.detail ? `, ${node.data.detail}` : ""}`}
          tabIndex={0}
          transform={`translate(${node.y}, ${node.x - minX + NODE_HEIGHT - NODE_HEIGHT / 2})`}
          onClick={() => onNodeActivate?.(node.data.id)}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              onNodeActivate?.(node.data.id);
            }
          }}
          className="cursor-default focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--focus,currentColor)]"
        >
          <rect
            width={NODE_WIDTH}
            height={NODE_HEIGHT}
            rx={6}
            fill="transparent"
            stroke="currentColor"
            strokeOpacity={0.35}
          />
          <text x={10} y={14} fontSize={11} fill="currentColor">
            {node.data.label}
          </text>
          {node.data.detail ? (
            <text x={10} y={27} fontSize={10} fill="currentColor" fillOpacity={0.7}>
              {node.data.detail.length > 30
                ? `${node.data.detail.slice(0, 29)}…`
                : node.data.detail}
            </text>
          ) : null}
        </g>
      ))}
    </svg>
  );
}
