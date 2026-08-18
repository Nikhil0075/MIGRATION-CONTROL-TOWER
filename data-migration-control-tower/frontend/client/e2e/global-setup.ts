import type { FullConfig } from "@playwright/test";
import type { Server } from "node:http";

const { startServer } = require("./server.cjs") as {
  startServer: () => Promise<Server>;
};

export default async function globalSetup(_config: FullConfig) {
  const server = await startServer();
  return async () =>
    new Promise<void>((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
}
