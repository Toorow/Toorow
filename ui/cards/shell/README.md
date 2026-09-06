# `@toorow/card-shell`

Two things live in this package, and the second is the one to read first.

## `src/viz/` — the shared Visualization runtime (Story 50.5)

One React rendering core. The Console and the share page consume it as ordinary
application code; an MCP host consumes it as one self-contained bundled resource.
Its single public entry is `VisualizationRuntime`, and the only thing it accepts is
the five-field envelope in `src/viz/contracts.ts`:

```
{ result, spec, pins, profile, display }
```

Raw renderer configuration — an ECharts `option`, a D3 selection instruction, a
function, a URL, a string of HTML — is refused wherever it arrives from and is
never persisted. The toorow compiler (`src/viz/compile/dataset.ts`) is the only
producer of a chart configuration, and `src/viz/adapters/echarts/` is the only
directory allowed to name one.

Layout:

| Path | What it is |
|---|---|
| `contracts.ts` | The envelope. Types only. |
| `validate.ts` | Refuses anything else, naming the offending field. |
| `registry.ts` | Family → renderer, as declarative data. Code, never a table. |
| `compile/` | Bindings + Result → dataset/encode. Reads values, computes none. |
| `adapters/echarts/` | The only producer of an ECharts option. |
| `renderers/` | The seven families, plus the D3 evidence path and the table fallback. |
| `evidence/` | One mapping from a mark back to the immutable Result. |
| `entries/` | Console, MCP App, share — same model, different profile. |
| `states.tsx` | The eleven shared non-success states. |

Build the two single-file bundles with `pnpm --filter @toorow/card-shell build`;
they land in `dist/viz/{mcp-app,share}.html` and are verified by
`node ui/scripts/bundle-check.mjs`.

### Why here, and not a fourth workspace package

Because AD-35 says so, by path. `_bmad-output/specs/spec-toorow/SPEC.md:162` names
"one shared Visualization runtime, bundled as a self-contained MCP App resource and
mounted directly by Console/Render surfaces", and `SPEC.md:164` identifies it as
`ui/cards/shell` — "they are complementary and use the same libraries, so they
migrate as one".

An earlier draft of Story 50.5 created a `ui/viz` package instead, on three
reasons that did not survive being checked:

- *"this package peer-depends on MUI, so the runtime would peer-depend on the
  library it forbids"* — that confuses the package **manifest** with the module
  **graph**. AD-35 forbids new code importing MUI, not a package whose other files
  still do. The real invariant is `grep -rn "@mui/\|@emotion/" src/viz`, which is
  empty and is asserted by `src/viz/__tests__/guards.test.ts`.
- *"a clean package lets the Console add one dependency with no MUI in its graph"*
  — true of the manifest graph, irrelevant to what has to be proven, and the
  Console already loads MUI in dozens of its own files.
- *"`SESSIONS.md` holds `ui/cards/**/vizTheme.ts`"* — false. No such row exists.

A fourth front-end package would also be a second place for the render vocabulary
to live, which is the failure `CLAUDE.md` §5 names.

## The twelve card primitives — superseded, not deleted

`LineChart`, `BarChart`, `Donut`, `Funnel`, `Gauge`, `Sparkline`, `MatrixHeatmap`,
`OverlayBarChart`, `ValueGrid`, `DotMatrix`, `RankedList`, `DataTable` are
hand-written SVG on MUI. The registry supersedes them **on the standard path**.
They stay on disk while the eight `ui/cards/*` apps still import them; retiring
those apps is a follow-on. Sixteen production files and seven test files in this
package still import `@mui/material` — that is declared debt, counted in Story
50.5's Dev Agent Record, and it may only go down.

`src/vizTheme.ts` is the single colour contract for both halves. It is already
MUI-free and reads the live CSS custom properties. Extend it in place; a second
palette module over a second variable set is forbidden.
