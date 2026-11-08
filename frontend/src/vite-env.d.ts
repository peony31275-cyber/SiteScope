/// <reference types="vite/client" />

// Public client configuration (frontend/.env.example). Never a secret: Vite embeds these values
// in the bundle.
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_POLL_INTERVAL_MS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
