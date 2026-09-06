/**
 * Story 50.5 -- build both single-file runtime bundles, one Vite invocation each.
 *
 * A node script rather than a shell one-liner: `VAR=x cmd && VAR=y cmd` is a
 * POSIX form, and this repository is developed on Windows. A build that only runs
 * on one developer's shell is a build nobody else can reproduce, which is the
 * opposite of what a pinned bundle is for.
 */
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pkgRoot = resolve(here, "..");

for (const entry of ["mcp-app", "share"]) {
  const result = spawnSync(
    process.execPath,
    [resolve(pkgRoot, "node_modules", "vite", "bin", "vite.js"), "build"],
    {
      cwd: pkgRoot,
      env: { ...process.env, TOOROW_VIZ_ENTRY: entry },
      stdio: "inherit",
    },
  );
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
