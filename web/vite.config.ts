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
const pdfJsWasmVendorRoute = "vendor/pdfjs/wasm";
const tesseractCoreFiles = [
  "tesseract-core-lstm.wasm.js",
  "tesseract-core-simd-lstm.wasm.js",
  "tesseract-core-relaxedsimd-lstm.wasm.js",
];
const pdfJsWasmFileNames = [
  "jbig2.wasm",
  "jbig2_nowasm_fallback.js",
  "openjpeg.wasm",
  "openjpeg_nowasm_fallback.js",
  "qcms_bg.wasm",
  "quickjs-eval.js",
  "quickjs-eval.wasm",
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

function pdfJsWasmAssetFiles(): Map<string, string> {
  const files = new Map<string, string>();
  const wasmDir = path.resolve(repoRoot, "node_modules/pdfjs-dist/wasm");
  for (const fileName of pdfJsWasmFileNames) {
    files.set(fileName, path.resolve(wasmDir, fileName));
  }
  return files;
}

function contentTypeFor(fileName: string): string {
  if (fileName.endsWith(".wasm")) return "application/wasm";
  if (fileName.endsWith(".js")) return "application/javascript";
  return "application/octet-stream";
}

function routePrefixes(base: string, vendorRoute: string): string[] {
  const normalizedBase = base.endsWith("/") ? base : `${base}/`;
  return Array.from(
    new Set([`/${vendorRoute}/`, `${normalizedBase}${vendorRoute}/`]),
  );
}

function serveVendorAsset(
  base: string,
  vendorRoute: string,
  files: Map<string, string>,
  req: IncomingMessage,
  res: ServerResponse,
  next: (err?: unknown) => void,
) {
  const requestPath = new URL(req.url || "/", "http://localhost").pathname;
  const prefix = routePrefixes(base, vendorRoute).find((candidate) =>
    requestPath.startsWith(candidate),
  );
  if (!prefix) {
    next();
    return;
  }

  const fileName = decodeURIComponent(requestPath.slice(prefix.length));
  const sourcePath = files.get(fileName);
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
      const files = tesseractAssetFiles();
      server.middlewares.use((req, res, next) => {
        serveVendorAsset(base, tesseractVendorRoute, files, req, res, next);
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

function pdfJsAssetsPlugin(base: string): Plugin {
  return {
    name: "local-pdfjs-assets",
    configureServer(server) {
      const files = pdfJsWasmAssetFiles();
      server.middlewares.use((req, res, next) => {
        serveVendorAsset(base, pdfJsWasmVendorRoute, files, req, res, next);
      });
    },
    closeBundle() {
      const outDir = path.resolve(distRoot, pdfJsWasmVendorRoute);
      fs.mkdirSync(outDir, { recursive: true });
      for (const [fileName, sourcePath] of pdfJsWasmAssetFiles()) {
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
    plugins: [
      react(),
      tailwindcss(),
      tesseractAssetsPlugin(base),
      pdfJsAssetsPlugin(base),
    ],
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
