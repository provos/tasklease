import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";
import { serviceVersion } from "../scripts/worker-config.mjs";

export default defineConfig({
  define: { TASKLEASE_VERSION: JSON.stringify(serviceVersion()) },
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          ADMIN_TOKEN: "test-admin-token",
          R2_ACCESS_KEY_ID: "test-r2-key",
          R2_SECRET_ACCESS_KEY: "test-r2-secret",
        },
      },
    }),
  ],
  test: { include: ["test/**/*.test.ts"] },
});
