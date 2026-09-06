import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { afterEach, test } from "node:test";

const CHECKER = fileURLToPath(new URL("./bundle-check.mjs", import.meta.url));
const temporaryRoots = [];

function temporaryRoot() {
  const root = mkdtempSync(join(tmpdir(), "toorow-bundle-check-"));
  temporaryRoots.push(root);
  return root;
}

function fixture(root, path, content) {
  const target = join(root, path);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, content);
  return target;
}

function run(target, ...args) {
  return spawnSync(process.execPath, [CHECKER, target, ...args], {
    encoding: "utf8",
  });
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

test("keeps file-target URL and size semantics", () => {
  const root = temporaryRoot();
  const clean = fixture(root, "clean.html", "<main>self contained</main>");
  const external = fixture(root, "external.html", '<script src="https://cdn.test/a.js">');

  assert.equal(run(clean, "--max-bytes", "100").status, 0);
  assert.match(run(clean, "--max-bytes", "5").stderr, /size gate/);
  assert.equal(run(external).status, 1);
});

test("fails closed on a missing, malformed or unknown budget argument", () => {
  const root = temporaryRoot();
  const clean = fixture(root, "clean.html", "<main>self contained</main>");

  assert.equal(run(clean, "--max-bytes").status, 2);
  assert.equal(run(clean, "--max-bytes", "10bytes").status, 2);
  assert.equal(run(clean, "--unknown", "10").status, 2);
});

for (const [extension, content] of [
  ["html", '<img src="https://cdn.test/image.png">'],
  ["css", ".hero { background: url(https://cdn.test/image.png) }"],
  ["js", 'fetch("https://api.test/data")'],
]) {
  test(`rejects a nested load-bearing external URL in ${extension}`, () => {
    const root = temporaryRoot();
    fixture(root, "index.html", "<main>clean shell</main>");
    fixture(root, `assets/nested/file.${extension}`, content);

    const result = run(root);
    assert.equal(result.status, 1);
    assert.match(result.stderr, new RegExp(`assets/nested/file\\.${extension}`));
  });
}

test("rejects unquoted and meta-refresh external HTML loads", () => {
  const root = temporaryRoot();
  fixture(root, "index.html", "<main>clean shell</main>");
  fixture(root, "nested/unquoted.html", "<img src=https://cdn.test/image.png>");
  fixture(
    root,
    "nested/refresh.html",
    '<meta http-equiv="refresh" content="0; url=https://external.test">',
  );

  const result = run(root);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /nested\/unquoted\.html/);
  assert.match(result.stderr, /nested\/refresh\.html/);
});

test("rejects external URLs that compiled code loads indirectly", () => {
  const root = temporaryRoot();
  fixture(root, "index.html", "<main>clean shell</main>");
  fixture(
    root,
    "assets/app.js",
    "const endpoint = `https://unexpected.test/oauth`; window.open(endpoint)",
  );

  const result = run(root);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /unexpected\.test/);
});

test("allows only the admin runtime origins ratified by the product", () => {
  const root = temporaryRoot();
  fixture(root, "index.html", "<main>clean shell</main>");
  fixture(
    root,
    "assets/app.js",
    [
      "const oauth = `https://api.nango.dev`; window.open(oauth)",
      "const script = document.createElement('script')",
      "script.src = `https://accounts.google.com/gsi/client`",
    ].join(";"),
  );

  assert.equal(run(root).status, 0);
});

test("rejects WebSocket origins that are not explicitly ratified", () => {
  const root = temporaryRoot();
  fixture(root, "index.html", "<main>clean shell</main>");
  fixture(root, "assets/live.js", "const socket = new WebSocket('wss://stream.test/events')");

  const result = run(root);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /wss:\/\/stream\.test/);
});

test("sums every regular file in a directory before applying the budget", () => {
  const root = temporaryRoot();
  fixture(root, "index.html", "1");
  fixture(root, "a.png", "123456");
  fixture(root, "nested/b.png", "123456");

  const result = run(root, "--max-bytes", "12");
  assert.equal(result.status, 1);
  assert.match(result.stderr, /13 bytes > 12 bytes/);
});

test("fails closed on an empty artifact directory", () => {
  const root = temporaryRoot();

  const result = run(root, "--max-bytes", "10");
  assert.equal(result.status, 1);
  assert.match(result.stderr, /artifact directory is empty/);
});

test("fails closed when a directory contains a symbolic link", () => {
  const root = temporaryRoot();
  const artifact = join(root, "artifact");
  const outside = join(root, "outside");
  mkdirSync(artifact);
  mkdirSync(outside);
  fixture(artifact, "index.html", "<main>clean shell</main>");
  fixture(outside, "index.html", "<main>outside</main>");
  symlinkSync(outside, join(artifact, "linked"), process.platform === "win32" ? "junction" : "dir");

  const result = run(artifact);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /symbolic links are not allowed/);
});

test("requires the admin entry point in a non-empty directory", () => {
  const root = temporaryRoot();
  fixture(root, "assets/app.js", "console.log('built')");

  const result = run(root);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /no root index\.html/);
});

test("fails closed on unknown file types and invalid UTF-8 text", () => {
  const unknown = temporaryRoot();
  fixture(unknown, "index.html", "<main>clean shell</main>");
  fixture(unknown, "release.payload", "https://unexpected.test");
  assert.match(run(unknown).stderr, /unsupported artifact file type/);

  const invalid = temporaryRoot();
  fixture(invalid, "index.html", Buffer.from([0xff, 0xfe, 0x3c, 0x00]));
  assert.match(run(invalid).stderr, /must be UTF-8/);
});

test("reports a clean directory's exact aggregate size", () => {
  const root = temporaryRoot();
  fixture(root, "index.html", "1234");
  fixture(root, "assets/app.js", "123456");

  const result = run(root, "--max-bytes", "10");
  assert.equal(result.status, 0);
  assert.match(result.stdout, /10 bytes <= 10 bytes/);
});
