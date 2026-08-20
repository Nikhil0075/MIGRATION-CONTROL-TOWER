import { idToken } from "./auth";
import { Envelope } from "./models";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<Envelope<T>> {
  const token = await idToken();
  const headers = new Headers(init.headers || {});
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...init, headers });
  const payload = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) {
    throw new ApiError(response.status, payload.detail || `Request failed with ${response.status}`);
  }
  return payload as Envelope<T>;
}

export async function publicApi<T = unknown>(path: string): Promise<Envelope<T>> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new ApiError(response.status, "Unable to load application configuration.");
  return (await response.json()) as Envelope<T>;
}

export async function authenticatedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = await idToken();
  const headers = new Headers(init.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(path, { ...init, headers });
}

export function idempotencyKey(prefix: string): string {
  return `${prefix}-${Date.now()}-${crypto.randomUUID()}`;
}
