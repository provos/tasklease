import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";
import { mockMonitorApi } from "./src/mock/plugin.ts";

// `--mode mock` serves /api/snapshot from mock scenarios (src/mock).
export default defineConfig(({ mode }) => ({
  plugins: [svelte(), ...(mode === "mock" ? [mockMonitorApi()] : [])],
  build: { license: { fileName: "THIRD_PARTY_NOTICES.md" } },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
}));
