import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built files go to frontend/dist and are committed, so the company PCs never need Node.
// Dev: `npm run dev` proxies /api to Django on :8000.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1200 },
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000", "/admin": "http://127.0.0.1:8000", "/accounts": "http://127.0.0.1:8000" } },
});
