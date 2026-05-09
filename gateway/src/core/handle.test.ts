import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { handle } from "./handle";
import type { Env } from "../domain/types";

const env: Env = {
  PORT: "3000",
  OCR_URL: "http://ocr.local:8000",
};

function withStaticFixture<T>(run: () => Promise<T>): Promise<T> {
  const previousCwd = process.cwd();
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

  process.chdir(tempRoot);
  return run().finally(() => {
    process.chdir(previousCwd);
    fs.rmSync(tempRoot, { recursive: true, force: true });
  });
}

test("handle serves GitHub Pages-prefixed assets from local dist", async () => {
  const response = await withStaticFixture(() =>
    handle(
      new Request("http://localhost/IttM/assets/static-handler-test.js"),
      env,
    ),
  );

  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /javascript/);
  assert.match(await response.text(), /ok = true/);
});

test("handle does not return index.html for missing asset files", async () => {
  const response = await withStaticFixture(() =>
    handle(new Request("http://localhost/IttM/assets/missing.js"), env),
  );

  assert.equal(response.status, 404);
  assert.match(response.headers.get("content-type") ?? "", /json/);
});
