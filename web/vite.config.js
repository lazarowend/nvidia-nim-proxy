import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy do dev-server: o frontend chama /health e /admin na MESMA origem
// (http://127.0.0.1:5173) e o Vite encaminha ao Flask em 5000.
// Isso elimina CORS sem tocar no backend.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/health": { target: "http://127.0.0.1:5000", changeOrigin: true },
      "/admin": { target: "http://127.0.0.1:5000", changeOrigin: true },
    },
  },
});
