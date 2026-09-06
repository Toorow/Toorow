/**
 * Capture the Platform Clocks screen.
 *
 * The payload below is NOT invented: its shape and its values are the real
 * `reconcile()` output measured against production on 2026-08-02 (seven clocks,
 * job prefix `toorow-`, region europe-west1). Two rows are then set to states
 * production did not happen to be in at that minute -- `unknown` and
 * `unmanaged_in_gcp` -- because those are precisely the ones a green test can
 * confirm while the screen still reads "everything is fine" to a human. Looking
 * at them is the point of the capture.
 *
 * It renders through the SCREEN SANDBOX (`/debug/screen?name=PlatformClocks`),
 * so `App.tsx` is never evaluated and the legacy stylesheet is never loaded:
 * the screen is judged on the component library alone (CLAUDE.md s5).
 */
// Playwright is NOT a dependency of this package: it is dev tooling, and adding
// it to ui/admin would put a browser download in every install of the console.
// Run this from a place that has it -- the repo keeps one in .ds-sync (gitignored):
//   node --experimental-... no: simply
//   cd .ds-sync && node ../ui/admin/scripts/capture-platform-clocks.mjs
// with a vite dev server already serving ui/admin on CAPTURE_BASE.
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const OUT = process.env.CAPTURE_OUT || 'C:/qt/shots';
const BASE = process.env.CAPTURE_BASE || 'http://localhost:5173';
mkdirSync(OUT, { recursive: true });

const declared = (schedule, target_path, purpose) => ({
  schedule,
  timezone: 'Europe/Paris',
  target_path,
  http_method: 'POST',
  attempt_deadline_seconds: 600,
  desired_state: 'enabled',
  purpose,
});
const observed = (schedule, target_path, extra = {}) => ({
  observed_at: '2026-08-02T13:24:11+00:00',
  state: 'enabled',
  schedule,
  timezone: 'Europe/Paris',
  target_uri: `https://mcp-server.example.run.app${target_path}`,
  http_method: 'POST',
  attempt_deadline_seconds: 600,
  last_attempt_at: '2026-08-02T13:20:00+00:00',
  last_attempt_status: 'code=200',
  ...extra,
});
const row = (clock_name, verdict, d, o, drift_detail = {}, observation_error = null) => ({
  clock_name, verdict, declared: d, observed: o, drift_detail, observation_error,
});

const CLOCKS = [
  row('dispatch-nightly', 'in_sync',
    declared('0 2 * * *', '/internal/scheduler/dispatch-nightly', 'Nightly datastream dispatch'),
    observed('0 2 * * *', '/internal/scheduler/dispatch-nightly')),
  // A drift, exactly as reconcile reported it when the job was hand-edited.
  row('poll-health', 'drifted',
    declared('0 6 * * *', '/internal/scheduler/poll-health', 'Daily connection health poll'),
    observed('0 7 * * *', '/internal/scheduler/poll-health'),
    { schedule: { declared: '0 6 * * *', observed: '0 7 * * *' } }),
  row('drain-outbox', 'in_sync',
    declared('*/5 * * * *', '/internal/scheduler/drain-outbox', 'Drain the operation outbox'),
    observed('*/5 * * * *', '/internal/scheduler/drain-outbox')),
  row('run-dq-monitors', 'in_sync',
    declared('*/15 * * * *', '/internal/scheduler/run-dq-monitors', 'Data-quality monitor sweep'),
    observed('*/15 * * * *', '/internal/scheduler/run-dq-monitors')),
  // Read, and the job is not there.
  row('reconcile-queues', 'missing_in_gcp',
    declared('*/10 * * * *', '/internal/scheduler/reconcile-queues', 'Re-dispatch pending work'),
    null),
  // Not read at all. Must NOT render as agreement.
  row('reconcile-clocks', 'unknown',
    declared('17 * * * *', '/internal/scheduler/reconcile-clocks', 'Observe the clocks themselves'),
    null, {}, 'Cloud Scheduler API refused the call: permission denied on projects/…/jobs'),
  // A job nobody declared. No declaration to edit, so no action is offered.
  row('legacy-hourly-sweep', 'unmanaged_in_gcp', null,
    observed('0 * * * *', '/internal/legacy/sweep')),
];

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1400 } });
const page = await ctx.newPage();

await page.route('**/api/platform/clocks**', (route) =>
  route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ clocks: CLOCKS }),
  }),
);

await page.goto(`${BASE}/debug/screen?name=PlatformClocks`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);
await page.screenshot({ path: `${OUT}/platform-clocks.png`, fullPage: true });

const seen = await page.evaluate(() => document.body.innerText.slice(0, 4000));
console.log('--- texte rendu (extrait) ---');
console.log(seen);

await browser.close();
console.log(`\nwrote ${OUT}/platform-clocks.png`);
