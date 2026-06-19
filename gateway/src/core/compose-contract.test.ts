import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const COMPOSE_PATH = new URL("../../../docker-compose.yml", import.meta.url);

test("compose services keep restart and healthcheck contracts for local smoke reliability", async () => {
  const compose = await readFile(COMPOSE_PATH, "utf8");

  for (const service of ["ocr", "gateway", "nginx"]) {
    const block = serviceBlock(compose, service);

    assert.match(block, /restart:\s+unless-stopped/);
    assert.match(block, /healthcheck:\n/);
    assert.match(block, /retries:\s+12/);
    assert.match(block, /timeout:\s+5s/);
  }
});

test("compose dependencies wait for healthy OCR and gateway instead of only process start", async () => {
  const compose = await readFile(COMPOSE_PATH, "utf8");
  const gateway = serviceBlock(compose, "gateway");
  const nginx = serviceBlock(compose, "nginx");

  assert.match(
    gateway,
    /depends_on:\n\s+ocr:\n\s+condition:\s+service_healthy/,
  );
  assert.match(
    nginx,
    /depends_on:\n\s+gateway:\n\s+condition:\s+service_healthy/,
  );
});

function serviceBlock(compose: string, service: string): string {
  const pattern = new RegExp(
    `\\n  ${service}:\\n([\\s\\S]*?)(?=\\n  [a-z0-9_-]+:|\\nvolumes:|$)`,
  );
  const match = pattern.exec(`\n${compose}`);
  assert.ok(match, `Missing compose service ${service}`);
  return match[1];
}
