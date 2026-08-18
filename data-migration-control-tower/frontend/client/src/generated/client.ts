import createClient from "openapi-fetch";
import { idToken } from "../auth";
import type { paths } from "./api-schema";

/** Generated-contract client for new feature modules.
 *
 * Existing pages retain the small Envelope helper while this client provides
 * compile-time path, query, request-body, and response checks directly from
 * FastAPI's OpenAPI document.
 */
export const generatedApi = createClient<paths>({ baseUrl: "" });

generatedApi.use({
  async onRequest({ request }) {
    const token = await idToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
});
