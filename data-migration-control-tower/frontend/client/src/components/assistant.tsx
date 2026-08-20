import { h } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { api, authenticatedFetch, idempotencyKey } from "../api";
import { Icon } from "./icons";

type Citation = { id: string; label: string; route: string };
type Message = { id: string; role: "user" | "assistant"; content: string; citations?: Citation[]; error?: string };

export function ControlTowerAssistant({
  open,
  estateId,
  route,
  onClose,
  onNavigate,
}: {
  open: boolean;
  estateId: string | null;
  route: string;
  onClose: () => void;
  onNavigate: (route: string) => void;
}) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [usePageContext, setUsePageContext] = useState(true);
  const abortRef = useRef<AbortController | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const questionRef = useRef<HTMLTextAreaElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    questionRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])",
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open]);

  async function ensureSession(): Promise<string> {
    if (sessionId) return sessionId;
    if (!estateId) throw new Error("Select an estate before asking the assistant.");
    const parts = route.split("/");
    const runId = usePageContext && parts[0] === "runs" ? parts[1] || null : null;
    const result = await api<any>("/api/v1/assistant/sessions", {
      method: "POST",
      body: JSON.stringify({ estate_id: estateId, route: usePageContext ? `/${route}` : "/overview", run_id: runId }),
    });
    setSessionId(result.data.session_id);
    return result.data.session_id;
  }

  function consumeEvent(block: string, assistantIndex: number) {
    const lines = block.split("\n");
    const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
    const raw = lines.filter((line) => line.startsWith("data:")) .map((line) => line.slice(5).trim()).join("\n");
    if (!event || !raw) return;
    const payload = JSON.parse(raw);
    setMessages((current) => current.map((message, index) => {
      if (index !== assistantIndex) return message;
      if (event === "delta") return { ...message, content: message.content + payload.text };
      if (event === "citations") return { ...message, citations: payload };
      if (event === "error") return { ...message, error: payload.detail };
      return message;
    }));
  }

  async function send(retryQuestion?: string) {
    const clean = (retryQuestion ?? question).trim();
    if (!clean || busy) return;
    const assistantIndex = messages.length + 1;
    const exchangeId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setMessages((current) => [
      ...current,
      { id: `${exchangeId}-user`, role: "user", content: clean },
      { id: `${exchangeId}-assistant`, role: "assistant", content: "" },
    ]);
    setQuestion("");
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const id = await ensureSession();
      const response = await authenticatedFetch(`/api/v1/assistant/sessions/${encodeURIComponent(id)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ question: clean }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error("The assistant could not start a response.");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";
        blocks.forEach((block) => consumeEvent(block, assistantIndex));
        if (done) break;
      }
      if (buffer.trim()) consumeEvent(buffer, assistantIndex);
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") {
        setMessages((current) => current.map((message, index) =>
          index === assistantIndex ? { ...message, error: (reason as Error).message } : message,
        ));
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
    }
  }

  async function clearConversation() {
    abortRef.current?.abort();
    if (sessionId) {
      await authenticatedFetch(`/api/v1/assistant/sessions/${encodeURIComponent(sessionId)}`, {
        method: "DELETE",
        headers: { "Idempotency-Key": idempotencyKey("assistant-delete") },
      }).catch(() => undefined);
    }
    setSessionId(null);
    setMessages([]);
  }

  if (!open) return null;
  return (
    <aside id="control-tower-assistant" ref={dialogRef} class="assistant-drawer" aria-label="Ask Control Tower" aria-modal="true" role="dialog">
      <header class="assistant-header">
        <div><p class="eyebrow">Gemini 3.5 Flash</p><h2>Ask Control Tower</h2></div>
        <button class="icon-button" onClick={onClose} aria-label="Close assistant"><Icon name="close" /></button>
      </header>
      <div class="assistant-boundary" role="note">
        Read-only answers from the active estate. The assistant cannot start, retry, approve or modify work.
      </div>
      <label class="assistant-context">
        <input type="checkbox" checked={usePageContext} disabled={Boolean(sessionId)} onChange={(event) => setUsePageContext(event.currentTarget.checked)} />
        Include current page and run context
      </label>
      <div class="assistant-messages" aria-live="polite">
        {!messages.length && (
          <div class="assistant-empty"><Icon name="agents" /><strong>What would you like to understand?</strong><span>Ask about run status, evidence, reconciliation, policies or agent decisions.</span></div>
        )}
        {messages.map((message, index) => (
          <article class={`assistant-message ${message.role}`} key={message.id}>
            <small>{message.role === "user" ? "You" : "Control Tower AI"}</small>
            <p>{message.content || (busy && index === messages.length - 1 ? "Reviewing authorized evidence…" : "")}</p>
            {message.error && <div class="inline-alert danger" role="alert">{message.error}</div>}
            {message.error && message.role === "assistant" && messages[index - 1]?.role === "user" && (
              <button class="assistant-retry" type="button" disabled={busy} onClick={() => void send(messages[index - 1].content)}>Retry question</button>
            )}
            {Boolean(message.citations?.length) && (
              <div class="assistant-citations" aria-label="Sources">
                {message.citations!.map((citation) => (
                  <button onClick={() => { onNavigate(String(citation.route).replace(/^\//, "")); onClose(); }}>[{citation.id}] {citation.label}</button>
                ))}
              </div>
            )}
          </article>
        ))}
      </div>
      <form class="assistant-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
        <label><span class="sr-only">Question</span><textarea ref={questionRef} value={question} maxLength={4000} rows={3} placeholder="Ask about this estate…" disabled={busy || !estateId} onInput={(event) => setQuestion(event.currentTarget.value)} /></label>
        <div><button type="button" class="button" onClick={() => void clearConversation()} disabled={!messages.length}>New conversation</button>{busy ? <button type="button" class="button button-critical" onClick={() => abortRef.current?.abort()}>Cancel</button> : <button type="submit" class="button button-primary" disabled={!question.trim() || !estateId}>Ask</button>}</div>
      </form>
    </aside>
  );
}
