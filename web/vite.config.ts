import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv } from "vite";

const webRoot = fileURLToPath(new URL(".", import.meta.url));
const repoRoot = path.resolve(webRoot, "..");

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, repoRoot, "");
  const base = env.VITE_BASE_PATH || "/";
  return {
    base,
    root: webRoot,
    plugins: [react(), tailwindcss()],
    define: {
      "process.env.GEMINI_API_KEY": JSON.stringify(env.GEMINI_API_KEY),
    },
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
      outDir: path.resolve(repoRoot, "dist"),
      emptyOutDir: true,
    },
  };
});
