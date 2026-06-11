import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv, type Plugin } from "vite";

const webRoot = fileURLToPath(new URL(".", import.meta.url));
const repoRoot = path.resolve(webRoot, "..");
const distRoot = path.resolve(repoRoot, "dist");
const tesseractVendorRoute = "vendor/tesseract";
const tesseractCoreFiles = [
  "tesseract-core-lstm.wasm.js",
  "tesseract-core-simd-lstm.wasm.js",
  "tesseract-core-relaxedsimd-lstm.wasm.js",
];

function tesseractAssetFiles(): Map<string, string> {
  const files = new Map<string, string>();
  files.set(
    "worker.min.js",
    path.resolve(repoRoot, "node_modules/tesseract.js/dist/worker.min.js"),
  );

  const coreDir = path.resolve(repoRoot, "node_modules/tesseract.js-core");
  for (const fileName of tesseractCoreFiles) {
    const sourcePath = path.resolve(coreDir, fileName);
    if (fs.existsSync(sourcePath)) {
      files.set(fileName, sourcePath);
    }
  }

  return files;
}

function contentTypeFor(fileName: string): string {
  if (fileName.endsWith(".wasm")) return "application/wasm";
  if (fileName.endsWith(".js")) return "application/javascript";
  return "application/octet-stream";
}

function routePrefixes(base: string): string[] {
  const normalizedBase = base.endsWith("/") ? base : `${base}/`;
  return Array.from(
    new Set([
      `/${tesseractVendorRoute}/`,
      `${normalizedBase}${tesseractVendorRoute}/`,
    ]),
  );
}

function serveTesseractAsset(
  base: string,
  req: IncomingMessage,
  res: ServerResponse,
  next: (err?: unknown) => void,
) {
  const requestPath = new URL(req.url || "/", "http://localhost").pathname;
  const prefix = routePrefixes(base).find((candidate) =>
    requestPath.startsWith(candidate),
  );
  if (!prefix) {
    next();
    return;
  }

  const fileName = decodeURIComponent(requestPath.slice(prefix.length));
  const sourcePath = tesseractAssetFiles().get(fileName);
  if (!sourcePath) {
    res.statusCode = 404;
    res.end("Not found");
    return;
  }

  res.setHeader("Content-Type", contentTypeFor(fileName));
  fs.createReadStream(sourcePath)
    .on("error", (err) => next(err))
    .pipe(res);
}

function tesseractAssetsPlugin(base: string): Plugin {
  return {
    name: "local-tesseract-assets",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        serveTesseractAsset(base, req, res, next);
      });
    },
    closeBundle() {
      const outDir = path.resolve(distRoot, tesseractVendorRoute);
      fs.mkdirSync(outDir, { recursive: true });
      for (const [fileName, sourcePath] of tesseractAssetFiles()) {
        fs.copyFileSync(sourcePath, path.resolve(outDir, fileName));
      }
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, repoRoot, "");
  const base = env.VITE_BASE_PATH || "/";
  return {
    base,
    root: webRoot,
    plugins: [react(), tailwindcss(), tesseractAssetsPlugin(base)],
    resolve: {
      alias: {
        "@": path.resolve(webRoot, "src"),
      },
    },
    server: {
      hmr: process.env.DISABLE_HMR !== "true",
      proxy: {
        "/api": {
          target: process.env.PORT
            ? `http://localhost:${process.env.PORT}`
            : "http://localhost:3000",
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: distRoot,
      emptyOutDir: true,
    },
  };
});
