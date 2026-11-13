import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    host: "0.0.0.0",
    port: 8090,
    proxy: {
      "/api": {
        target: "http://localhost:8090",
        changeOrigin: true,
      },
    },
  },
});
