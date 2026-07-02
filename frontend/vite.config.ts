import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:18820",
      "/hermes": { target: "ws://127.0.0.1:18810", ws: true },
      "/onebot": { target: "ws://127.0.0.1:18800", ws: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
