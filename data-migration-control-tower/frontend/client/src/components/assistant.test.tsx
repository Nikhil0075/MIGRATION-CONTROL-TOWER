import { cleanup, fireEvent, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const fetchMock = vi.fn();

vi.mock("../api", () => ({
  api: (...args: unknown[]) => apiMock(...args),
  authenticatedFetch: (...args: unknown[]) => fetchMock(...args),
  idempotencyKey: () => "assistant-test-key",
}));

import { ControlTowerAssistant } from "./assistant";

function assistant(overrides: Record<string, unknown> = {}) {
  const props = {
    open: true,
    estateId: "estate-a",
    route: "runs/run-1",
    onClose: vi.fn(),
    onNavigate: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<ControlTowerAssistant {...(props as any)} />) };
}

function streamResponse(): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(
        'event: citations\ndata: [{"id":"run","label":"run-1","route":"/runs/run-1"}]\n\n'
        + 'event: delta\ndata: {"text":"The run is complete [run]."}\n\n'
        + 'event: done\ndata: {"message_id":"message-1"}\n\n',
      ));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

beforeEach(() => {
  apiMock.mockReset();
  fetchMock.mockReset();
  apiMock.mockResolvedValue({ data: { session_id: "session-1" } });
  fetchMock.mockResolvedValue(streamResponse());
});

afterEach(cleanup);

describe("Ask Control Tower", () => {
  it("states the read-only boundary and disables questions without an estate", () => {
    assistant({ estateId: null });
    expect(screen.getByText(/cannot start, retry, approve or modify work/i)).toBeTruthy();
    expect((screen.getByPlaceholderText("Ask about this estate…") as HTMLTextAreaElement).disabled).toBe(true);
  });

  it("creates an estate-scoped session and streams a cited answer", async () => {
    assistant();
    fireEvent.input(screen.getByPlaceholderText("Ask about this estate…"), { target: { value: "What happened?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText("The run is complete [run].")).toBeTruthy();
    expect(apiMock).toHaveBeenCalledWith("/api/v1/assistant/sessions", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ estate_id: "estate-a", route: "/runs/run-1", run_id: "run-1" }),
    }));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/assistant/sessions/session-1/messages",
      expect.objectContaining({ method: "POST" }),
    );
    expect(screen.getByRole("button", { name: "[run] run-1" })).toBeTruthy();
  });

  it("navigates citations inside the console", async () => {
    const { props } = assistant();
    fireEvent.input(screen.getByPlaceholderText("Ask about this estate…"), { target: { value: "Status?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    fireEvent.click(await screen.findByRole("button", { name: "[run] run-1" }));
    expect(props.onNavigate).toHaveBeenCalledWith("runs/run-1");
    expect(props.onClose).toHaveBeenCalled();
  });

  it("closes with Escape", () => {
    const { props } = assistant();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(props.onClose).toHaveBeenCalled();
  });

  it("offers retry after an unavailable response", async () => {
    fetchMock.mockRejectedValueOnce(new Error("Assistant unavailable"));
    assistant();
    fireEvent.input(screen.getByPlaceholderText("Ask about this estate…"), { target: { value: "Explain failure" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByRole("button", { name: "Retry question" })).toBeTruthy();
  });
});
