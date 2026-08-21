import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: "operator.js",
        assetFileNames: (asset) => asset.names.some((name) => name.endsWith(".css"))
          ? "operator.css"
          : "[name][extname]",
      },
    },
  },
});
