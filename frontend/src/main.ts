/**
 * Daisy frontend entry point (Vanilla TypeScript).
 */
const app = document.querySelector<HTMLElement>("#app");

if (app) {
  app.dataset.ready = "true";
}

export {};
