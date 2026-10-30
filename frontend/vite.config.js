import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` proxies API calls to a gateway on port 8000, so the browser
// sees one origin, as it does behind Caddy in Docker.
const gateway = process.env.GATEWAY_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/v1": gateway,
      "/docs": gateway,
      "/redoc": gateway,
      "/openapi.json": gateway,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.js"],
  },
});
