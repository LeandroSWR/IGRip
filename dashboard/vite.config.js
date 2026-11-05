import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Python server is the API. During `npm run dev` Vite forwards /api to it.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
});
