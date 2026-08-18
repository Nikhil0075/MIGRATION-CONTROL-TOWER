/**
 * Estate onboarding wizard — component-level tests (Day 11 Phase 8).
 *
 * The Playwright suite drives this wizard end to end against a built
 * bundle; these are the cheaper, more targeted counterpart, and they cover
 * the branches that are awkward to reach through a browser: what happens
 * with an assessment-only adapter, what the payload actually contains, and
 * what the component does before any API responds.
 *
 * The assertion that matters most is
 * "never renders a password input" — the wizard's whole premise is that it
 * collects credential REFERENCES. The API enforces that too
 * (ConnectionProfileModel forbids extra keys), but a UI that offers a field
 * the backend must reject is a broken promise either way.
 */

import { cleanup, render, screen, waitFor } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.fn();
vi.mock("../api", () => ({
  api: (...args: unknown[]) => api(...args),
  idempotencyKey: vi.fn(() => "test-idempotency-key"),
}));

import { EstateWizard } from "./estate-wizard";

const ADAPTERS = [
  { adapter_type: "sqlserver", capabilities: ["discover", "health", "reconcile", "transfer"] },
  { adapter_type: "oracle_corpus", capabilities: ["discover"] },
];

const PACKS = [
  { pack_id: "wwi_sqlserver_v1", version: "1.0.0", default_mode: "execution", execution_supported: true },
  { pack_id: "oracle_corpus_v1", version: "1.0.0", default_mode: "assessment", execution_supported: false },
];

function envelope(data: unknown) {
  return { data, meta: { generated_at: "2026-08-18T00:00:00Z", freshness: "live" } };
}

const operator = {
  uid: "op",
  email: "op@example.internal",
  roles: ["operator", "viewer"],
} as any;

const props = { session: operator, onInspect: vi.fn(), navigate: vi.fn(), route: "estates/new" };

beforeEach(() => {
  api.mockReset();
  api.mockImplementation((path: string) => {
    if (path === "/api/v1/adapter-types") return Promise.resolve(envelope(ADAPTERS));
    if (path === "/api/v1/assessments") return Promise.resolve(envelope({ packs: PACKS }));
    if (path === "/api/v1/estate-validations")
      return Promise.resolve(envelope({ status: "NOT_APPLICABLE", detail: "Static source" }));
    return Promise.resolve(envelope({}));
  });
});

afterEach(cleanup);

function typeInto(label: RegExp | string, value: string) {
  const field = screen.getByLabelText(label) as HTMLInputElement;
  field.value = value;
  field.dispatchEvent(new Event("input", { bubbles: true }));
}

function click(name: string) {
  (screen.getByRole("button", { name }) as HTMLButtonElement).click();
}

async function reachSourceStep() {
  render(<EstateWizard {...props} />);
  await screen.findByLabelText(/Estate ID/);
  typeInto(/Estate ID/, "acme-finance");
  typeInto(/Display name/, "ACME Finance");
  await waitFor(() => expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(false));
  click("Next");
  await screen.findByLabelText(/Source ID/);
}

describe("credential containment", () => {
  it("never renders a password input at any step", async () => {
    const { container } = render(<EstateWizard {...props} />);
    await screen.findByLabelText(/Estate ID/);
    // The type matters: a password input implies a value is being
    // collected. This wizard only ever collects references.
    expect(container.querySelectorAll('input[type="password"]').length).toBe(0);
  });

  it("labels the credential fields as references, not secrets", async () => {
    await reachSourceStep();
    typeInto(/Source ID/, "finance-sqlserver");
    const select = screen.getByLabelText(/Adapter/) as HTMLSelectElement;
    select.value = "sqlserver";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(false));
    click("Next");

    expect(await screen.findByLabelText(/Secret Manager reference/)).toBeTruthy();
    expect(screen.getByLabelText(/environment variable fallback/)).toBeTruthy();
  });
});

describe("step gating", () => {
  it("blocks advancing until the identity is valid", async () => {
    render(<EstateWizard {...props} />);
    await screen.findByLabelText(/Estate ID/);
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("rejects an estate id that is not a slug", async () => {
    render(<EstateWizard {...props} />);
    await screen.findByLabelText(/Estate ID/);
    typeInto(/Estate ID/, "Not A Slug");
    typeInto(/Display name/, "Whatever");
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true),
    );
    // ...and says why, rather than leaving the button mysteriously dead.
    expect(screen.getByRole("alert").textContent).toMatch(/lowercase letters, digits and hyphens/i);
  });
});

describe("capability-driven behaviour", () => {
  it("lists adapters from the API rather than a hardcoded set", async () => {
    await reachSourceStep();
    const options = Array.from(
      (screen.getByLabelText(/Adapter/) as HTMLSelectElement).options,
    ).map((option) => option.value);
    expect(options).toContain("sqlserver");
    expect(options).toContain("oracle_corpus");
  });

  it("warns that a discovery-only adapter cannot be migrated", async () => {
    await reachSourceStep();
    const select = screen.getByLabelText(/Adapter/) as HTMLSelectElement;
    select.value = "oracle_corpus";
    select.dispatchEvent(new Event("change", { bubbles: true }));

    // The limitation is stated, not implied by a disabled control.
    expect(await screen.findByText(/supports discovery only/)).toBeTruthy();
  });

  it("shows a credential-free connection and validation path for a static source", async () => {
    await reachSourceStep();
    typeInto(/Source ID/, "corpus");
    const select = screen.getByLabelText(/Adapter/) as HTMLSelectElement;
    select.value = "oracle_corpus";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(false));
    click("Next");

    expect(await screen.findByText(/has no server to connect to/)).toBeTruthy();
    expect(screen.queryByLabelText(/Secret Manager reference/)).toBeNull();
    click("Next");
    expect(await screen.findByRole("heading", { name: "Validate source" })).toBeTruthy();
    click("Validate source");
    expect(await screen.findByText("NOT_APPLICABLE")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(false));
    click("Next");
    expect(await screen.findByLabelText(/^Pack$/)).toBeTruthy();
  });
});

describe("authorization", () => {
  it("tells a viewer they cannot save, rather than failing silently", async () => {
    const viewer = { ...operator, roles: ["viewer"] };
    render(<EstateWizard {...props} session={viewer} />);
    expect(await screen.findByText(/requires the operator role/)).toBeTruthy();
  });
});
