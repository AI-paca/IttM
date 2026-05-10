import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { serveStaticFile } from "./staticFiles";

function withStaticFixture<T>(
  run: (distRoot: string) => Promise<T>,
): Promise<T> {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ittm-static-"));
  const distRoot = path.join(tempRoot, "dist");
  const assetsRoot = path.join(distRoot, "assets");
  fs.mkdirSync(assetsRoot, { recursive: true });
  fs.writeFileSync(
    path.join(distRoot, "index.html"),
    '<!doctype html><div id="root"></div>',
  );
  fs.writeFileSync(
    path.join(assetsRoot, "static-handler-test.js"),
    "export const ok = true;",
  );

  return run(distRoot).finally(() => {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  });
}

test("serveStaticFile serves GitHub Pages-prefixed assets without a MIME map", async () => {
  const response = await withStaticFixture((distRoot) =>
    serveStaticFile(
      new Request("http://localhost/IttM/assets/static-handler-test.js"),
      { distRoot },
    ),
  );

  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /javascript/);
  assert.match(await response.text(), /ok = true/);
});

test("serveStaticFile uses SPA fallback only for non-asset paths", async () => {
  const response = await withStaticFixture((distRoot) =>
    serveStaticFile(new Request("http://localhost/IttM/configure"), {
      distRoot,
    }),
  );

  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /html/);
  assert.match(await response.text(), /root/);
});

test("serveStaticFile does not return index.html for missing asset files", async () => {
  const response = await withStaticFixture((distRoot) =>
    serveStaticFile(new Request("http://localhost/IttM/assets/missing.js"), {
      distRoot,
    }),
  );

  assert.equal(response.status, 404);
  assert.match(response.headers.get("content-type") ?? "", /json/);
});
