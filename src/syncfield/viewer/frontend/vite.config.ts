import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/ws": { target: "ws://localhost:8420", ws: true },
      "/api": "http://localhost:8420",
      "/stream": "http://localhost:8420",
    },
  },
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
});
