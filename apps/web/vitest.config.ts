import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL("./core", import.meta.url)) } },
  test: {
    environment: "node",
    include: [
      "core/components/builds/**/*.test.ts",
      "core/components/teams/**/*.test.ts",
      "core/components/infrastructure/**/*.test.{ts,tsx}",
    ],
  },
});
