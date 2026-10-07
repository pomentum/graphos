import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/** graphos API the dev server forwards `/api` to; nginx does the same in the image. */
const apiTarget = process.env.GRAPHOS_API_URL ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  build: {
    target: "es2022",
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        // Libraries change less often than the app: separate files cache better.
        manualChunks: {
          graph: ["sigma", "graphology", "graphology-layout", "graphology-layout-forceatlas2"],
          mantine: ["@mantine/core", "@mantine/hooks", "@mantine/notifications"],
          react: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
