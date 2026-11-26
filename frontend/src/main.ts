/**
 * Daisy frontend entry point (Vanilla TypeScript).
 * Split-view scaffold: chat pane (left) and artifacts pane (right).
 */

import { invoke } from "@tauri-apps/api/core";

interface ChatMessage {
  role: "user" | "assistant";
  text: string;
}

interface SidecarConnection {
  port: number;
  token: string;
}

const CHAT_MODEL = "qwen2.5-coder:7b";
const DEV_CONNECTION_KEY = "daisy.devConnection";

/**
 * SSE event boundary per the spec: a blank line, where a line ends in CRLF, LF, or CR.
 * sse-starlette defaults to CRLF, so a plain "\n\n" split never matches.
 */
const SSE_BOUNDARY = /\r\n\r\n|\n\n|\r\r/;

const messages: ChatMessage[] = [];
let connection: SidecarConnection | null = null;
let streaming = false;

function requireEl<T extends HTMLElement>(selector: string): T {
  const el = document.querySelector<T>(selector);
  if (!el) {
    throw new Error(`Missing required element: ${selector}`);
  }
  return el;
}

const app = requireEl<HTMLElement>("#app");
const list = requireEl<HTMLElement>("#chat-messages");
const placeholder = requireEl<HTMLElement>("#chat-placeholder");
const form = requireEl<HTMLFormElement>("#chat-form");
const input = requireEl<HTMLTextAreaElement>("#chat-input");
const sendButton = requireEl<HTMLButtonElement>("#chat-send");
const toggle = requireEl<HTMLButtonElement>("#artifact-toggle");

function isTauriRuntime(): boolean {
  return "__TAURI_INTERNALS__" in window;
}

/** Resolves the sidecar port and token: Tauri invoke first, then a dev fallback in localStorage. */
async function loadConnection(): Promise<SidecarConnection | null> {
  if (isTauriRuntime()) {
    return invoke<SidecarConnection>("get_sidecar_connection");
  }
  const raw = localStorage.getItem(DEV_CONNECTION_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Partial<SidecarConnection>;
    if (typeof parsed.port === "number" && typeof parsed.token === "string") {
      return { port: parsed.port, token: parsed.token };
    }
  } catch {
    // Ignore malformed dev settings and fall through to the offline notice.
  }
  return null;
}

function createBubble(msg: ChatMessage): HTMLElement {
  placeholder.hidden = true;
  const item = document.createElement("div");
  item.className = `chat-message chat-message--${msg.role}`;
  item.textContent = msg.text;
  list.appendChild(item);
  scrollToBottom();
  return item;
}

function scrollToBottom(): void {
  list.scrollTo({ top: list.scrollHeight, behavior: "smooth" });
}

function setStreaming(active: boolean): void {
  streaming = active;
  sendButton.disabled = active;
  input.disabled = active;
}

/** Parses one complete SSE block into its event name and data payload. Accepts CRLF, LF, or CR line endings. */
function parseSseBlock(block: string): { event: string; data: string } | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split(/\r\n|\n|\r/)) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).replace(/^ /, ""));
    }
    // Lines starting with ":" are comments (keep-alive pings) and are ignored.
  }
  if (dataLines.length === 0) {
    return null;
  }
  return { event, data: dataLines.join("\n") };
}

function readField(data: string, key: string): string {
  try {
    const parsed = JSON.parse(data) as Record<string, unknown>;
    const value = parsed[key];
    return typeof value === "string" ? value : "";
  } catch {
    return data;
  }
}

/** Streams the assistant reply for `text` into `bubble`, updating live as tokens arrive. */
async function streamReply(text: string, bubble: HTMLElement): Promise<void> {
  if (!connection) {
    bubble.textContent = "Backend is not connected.";
    bubble.classList.add("chat-message--error");
    return;
  }
  const response = await fetch(`http://127.0.0.1:${connection.port}/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${connection.token}`,
    },
    body: JSON.stringify({ message: text, model: CHAT_MODEL }),
  });
  if (!response.ok || !response.body) {
    throw new Error(`Chat request failed with HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  bubble.classList.add("chat-message--streaming");

  const handleEvent = (event: string, data: string): void => {
    if (event === "token") {
      bubble.textContent = (bubble.textContent ?? "") + readField(data, "content");
      scrollToBottom();
    } else if (event === "status") {
      if (readField(data, "message") === "done") {
        finished = true;
      }
    } else if (event === "done") {
      finished = true;
    } else if (event === "error") {
      const message = readField(data, "message") || "Unknown error";
      bubble.classList.add("chat-message--error");
      bubble.textContent = (bubble.textContent ?? "") + `\n[error] ${message}`;
      finished = true;
    }
  };

  /** Emits every complete event in `buffer` and keeps any trailing partial event. */
  const drainBuffer = (): void => {
    let match = SSE_BOUNDARY.exec(buffer);
    while (match) {
      const block = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      const parsed = parseSseBlock(block);
      if (parsed) {
        handleEvent(parsed.event, parsed.data);
      }
      if (finished) {
        return;
      }
      match = SSE_BOUNDARY.exec(buffer);
    }
  };

  while (!finished) {
    const { value, done } = await reader.read();
    if (done) {
      buffer += decoder.decode();
      drainBuffer();
      if (!finished && buffer.trim() !== "") {
        const parsed = parseSseBlock(buffer);
        if (parsed) {
          handleEvent(parsed.event, parsed.data);
        }
      }
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    drainBuffer();
  }
  await reader.cancel().catch(() => undefined);
  bubble.classList.remove("chat-message--streaming");
  if (!finished && !bubble.classList.contains("chat-message--error")) {
    bubble.classList.add("chat-message--error");
    bubble.textContent = (bubble.textContent ?? "") + "\n[error] Stream ended before completion";
  }
}

async function submitMessage(): Promise<void> {
  const text = input.value.trim();
  if (text === "" || streaming) {
    return;
  }
  const userMessage: ChatMessage = { role: "user", text };
  messages.push(userMessage);
  createBubble(userMessage);
  input.value = "";
  autosize();

  const reply: ChatMessage = { role: "assistant", text: "" };
  messages.push(reply);
  const bubble = createBubble(reply);
  bubble.classList.add("chat-message--pending");

  setStreaming(true);
  try {
    await streamReply(text, bubble);
  } catch (error) {
    bubble.classList.add("chat-message--error");
    bubble.textContent = `Could not reach Daisy: ${error instanceof Error ? error.message : String(error)}`;
  } finally {
    bubble.classList.remove("chat-message--pending");
    setStreaming(false);
    scrollToBottom();
  }
}

function autosize(): void {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

form.addEventListener("submit", (event: SubmitEvent) => {
  event.preventDefault();
  void submitMessage();
});

input.addEventListener("keydown", (event: KeyboardEvent) => {
  // Enter sends; Shift+Enter inserts a newline.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void submitMessage();
  }
});

input.addEventListener("input", autosize);

toggle.addEventListener("click", () => {
  const collapsed = app.dataset.collapsed !== "true";
  app.dataset.collapsed = collapsed ? "true" : "false";
  toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  toggle.textContent = collapsed ? "Expand" : "Collapse";
});

async function init(): Promise<void> {
  try {
    connection = await loadConnection();
  } catch (error) {
    console.error("Failed to load sidecar connection", error);
    connection = null;
  }
  if (!connection) {
    placeholder.textContent =
      "Backend not connected. In browser dev mode, set localStorage key daisy.devConnection to {\"port\":<port>,\"token\":\"<token>\"}.";
  }
  app.dataset.ready = "true";
}

void init();

export {};
