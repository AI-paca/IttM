import { readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

export function discoverTests(directory) {
  return readdirSync(directory, { withFileTypes: true })
    .flatMap((entry) => {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) return discoverTests(path);
      return entry.isFile() && /\.test\.(ts|mjs)$/.test(entry.name)
        ? [path]
        : [];
    })
    .sort();
}

if (resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = fileURLToPath(new URL("../../", import.meta.url));
  const tests = ["web/src", "gateway/src", "edge", "scripts/ci"].flatMap(
    (directory) => discoverTests(join(root, directory)),
  );
  console.log(`Running ${tests.length} unit test files`);
  const result = spawnSync(
    process.execPath,
    ["--import", "tsx", "--test", ...tests],
    { cwd: root, stdio: "inherit" },
  );
  if (result.error) throw result.error;
  process.exit(result.status ?? 1);
}
