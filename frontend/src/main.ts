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

interface HealthReport {
  ok: boolean;
  detail: string;
}

interface RollbackOutcome {
  snapshotId: string;
  port: number;
  token: string;
}

type SidecarStatus = "connecting" | "reloading" | "connected" | "offline" | "error";

declare global {
  interface Window {
    /** Set by artifacts.ts once the artifacts panel has loaded. */
    daisyArtifacts?: {
      stageProposal(files: Record<string, { before: string; after: string }>): Promise<string | null>;
      rejudge(proposalId: string): Promise<void>;
    };
  }
}

const CHAT_MODEL = "qwen2.5-coder:7b";
const DEV_CONNECTION_KEY = "daisy.devConnection";

/**
 * SSE event boundary per the spec: a blank line, where a line ends in CRLF, LF, or CR.
 * sse-starlette defaults to CRLF, so a plain "\n\n" split never matches.
 */
const SSE_BOUNDARY = /\r\n\r\n|\n\n|\r\r/;

/**
 * Primary proposal fence: ```daisy-proposal path=<relative path>, then the body, then a closing
 * fence line. The path may be bare or wrapped in single or double quotes.
 */
const PROPOSAL_FENCE = /```daisy-proposal[ \t]+path=("[^"\n]+"|'[^'\n]+'|[^\s`]+)[ \t]*\r?\n([\s\S]*?)\r?\n```/g;

/** Fallback fence: a python block. Its target is found from a leading comment or nearby prose. */
const PYTHON_FENCE = /```(?:python|py)[ \t]*\r?\n([\s\S]*?)\r?\n```/g;

/** Leading comment that names the target file, e.g. `# file: extensions/x.py`. */
const PATH_COMMENT = /^\s*#\s*(?:file|path|filepath)\s*:\s*(\S+)/i;

/** A workspace path under extensions/ ending in .py, found in prose or in the first lines of code. */
const EXTENSION_PATH = /(?:^|[\s`'"(])(extensions\/[\w\-./]+\.py)\b/g;

/** How much text before a python block is searched for a target path. */
const PRECEDING_WINDOW_CHARS = 240;

const messages: ChatMessage[] = [];
let connection: SidecarConnection | null = null;
let streaming = false;
let reloading = false;
let recovering = false;

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
const statusEl = requireEl<HTMLElement>("#sidecar-status");
const reloadButton = requireEl<HTMLButtonElement>("#sidecar-reload");
const rollbackBanner = requireEl<HTMLElement>("#rollback-banner");
const rollbackText = requireEl<HTMLElement>("#rollback-banner-text");
const rollbackDismiss = requireEl<HTMLButtonElement>("#rollback-banner-dismiss");

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

/** Updates the header status text and its data-state, which drives the status styling. */
function setStatus(state: SidecarStatus, detail?: string): void {
  const labels: Record<SidecarStatus, string> = {
    connecting: "Connecting",
    reloading: "Reloading…",
    connected: "Connected",
    offline: "Offline",
    error: "Error",
  };
  const suffix = state === "connected" && connection ? ` · port ${connection.port}` : "";
  statusEl.dataset.state = state === "connected" ? "connected" : state === "error" ? "error" : "idle";
  statusEl.textContent = `${labels[state]}${suffix}`;
  statusEl.title = detail ?? "";
}

/** Shows the rollback notice. `kind` picks the styling: a recovered rollback or a failed one. */
function showRollbackNotice(message: string, kind: "recovered" | "failed"): void {
  rollbackText.textContent = message;
  rollbackBanner.dataset.kind = kind;
  rollbackBanner.hidden = false;
}

function hideRollbackNotice(): void {
  rollbackBanner.hidden = true;
  rollbackText.textContent = "";
}

/** Disables controls while a reload, a recovery, or a reply is in flight. */
function refreshControls(): void {
  const busy = reloading || recovering || streaming;
  reloadButton.disabled = busy;
  sendButton.disabled = busy;
  input.disabled = busy;
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
  refreshControls();
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

/**
 * Accepts only workspace-relative paths. Absolute paths, backslashes and ".." segments are
 * rejected here as a first line of defence; the backend validates the paths again.
 */
function cleanPath(raw: string): string | null {
  let path = raw.trim();
  if (path.length >= 2 && (path[0] === '"' || path[0] === "'") && path[path.length - 1] === path[0]) {
    path = path.slice(1, -1).trim();
  }
  if (path === "" || path.startsWith("/") || path.indexOf("\\") !== -1) {
    return null;
  }
  const segments = path.split("/");
  for (const segment of segments) {
    if (segment === ".." || segment === "") {
      return null;
    }
  }
  return path;
}

/** Returns the last extensions/*.py path mentioned in `text`, or null. */
function lastExtensionPath(text: string): string | null {
  const pattern = new RegExp(EXTENSION_PATH.source, "g");
  let found: string | null = null;
  let match = pattern.exec(text);
  while (match) {
    found = match[1] ?? null;
    match = pattern.exec(text);
  }
  return found === null ? null : cleanPath(found);
}

/**
 * Works out the target path for a python block. Order: a leading path comment in the block,
 * then an extensions/*.py path in the first lines of the block, then one in the prose just
 * before the block.
 */
function pythonBlockTarget(body: string, preceding: string): string | null {
  const lines = body.split(/\r?\n/);
  const firstLine = lines[0] ?? "";
  const comment = PATH_COMMENT.exec(firstLine);
  if (comment && comment[1]) {
    const fromComment = cleanPath(comment[1]);
    if (fromComment) {
      return fromComment;
    }
  }
  const head = lines.slice(0, 3).join("\n");
  const fromHead = lastExtensionPath(head);
  if (fromHead) {
    return fromHead;
  }
  return lastExtensionPath(preceding.slice(-PRECEDING_WINDOW_CHARS));
}

/**
 * Extracts file proposals from a finished reply. Returns a map of relative path to full proposed
 * content. The primary `daisy-proposal` fence is read first. Python fences are a fallback, and
 * only count when a target path can be identified. A path repeated in one reply keeps its last block.
 */
export function extractProposalBlocks(text: string): Record<string, string> {
  const files: Record<string, string> = {};

  const primary = new RegExp(PROPOSAL_FENCE.source, "g");
  let match = primary.exec(text);
  while (match) {
    const path = cleanPath(match[1] ?? "");
    const body = match[2] ?? "";
    if (path) {
      files[path] = body;
    }
    match = primary.exec(text);
  }

  const python = new RegExp(PYTHON_FENCE.source, "g");
  match = python.exec(text);
  while (match) {
    const body = match[1] ?? "";
    const preceding = text.slice(0, match.index);
    const path = pythonBlockTarget(body, preceding);
    if (path && files[path] === undefined) {
      files[path] = body;
    }
    match = python.exec(text);
  }

  return files;
}

/**
 * Stages the proposals in a finished reply for review in the artifacts panel. Each distinct
 * path and content pair is staged once per turn, so a block repeated in the reply is not staged
 * twice. The original file content is not loaded, so the diff shows the proposed content as
 * additions. Nothing is written here; the reviewer must approve in the artifacts panel.
 */
async function stageProposalsFromReply(text: string, stagedKeys: Set<string>): Promise<void> {
  const blocks = extractProposalBlocks(text);
  const fresh: Record<string, { before: string; after: string }> = {};
  let count = 0;
  for (const path of Object.keys(blocks)) {
    const after = blocks[path] ?? "";
    const key = `${path}\u0000${after}`;
    if (stagedKeys.has(key)) {
      continue;
    }
    stagedKeys.add(key);
    fresh[path] = { before: "", after };
    count++;
  }
  if (count === 0) {
    return;
  }
  const stage = window.daisyArtifacts?.stageProposal;
  if (!stage) {
    console.warn("Proposal blocks found but the artifacts panel is not loaded");
    return;
  }
  await stage(fresh);
}

/** Streams the assistant reply for `text` into `bubble`, updating live as tokens arrive. */
async function streamReply(text: string, bubble: HTMLElement): Promise<void> {
  // Read the connection at send time so a reload that finished earlier is picked up.
  const active = connection;
  if (!active) {
    bubble.textContent = "Backend is not connected.";
    bubble.classList.add("chat-message--error");
    return;
  }
  const response = await fetch(`http://127.0.0.1:${active.port}/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${active.token}`,
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
  if (text === "" || streaming || reloading || recovering) {
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

  // Keys of proposals already staged for this turn. Scoped to one turn, so a fresh message can
  // propose the same content again.
  const stagedKeys = new Set<string>();

  setStreaming(true);
  try {
    await streamReply(text, bubble);
    if (!bubble.classList.contains("chat-message--error")) {
      await stageProposalsFromReply(bubble.textContent ?? "", stagedKeys);
    }
  } catch (error) {
    bubble.classList.add("chat-message--error");
    bubble.textContent = `Could not reach Daisy: ${error instanceof Error ? error.message : String(error)}`;
  } finally {
    bubble.classList.remove("chat-message--pending");
    setStreaming(false);
    scrollToBottom();
  }
}

/**
 * Restarts the Python sidecar through Tauri IPC and swaps in the new port and token.
 * Chat is locked while the reload runs, so no request uses the old connection mid-swap.
 */
async function reloadSidecar(): Promise<void> {
  if (!isTauriRuntime() || reloading || recovering || streaming) {
    return;
  }
  reloading = true;
  refreshControls();
  setStatus("reloading");
  try {
    const next = await invoke<SidecarConnection>("reload_sidecar");
    connection = { port: next.port, token: next.token };
    setStatus("connected");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error("Sidecar reload failed", error);
    setStatus("error", message);
  } finally {
    reloading = false;
    refreshControls();
  }
}

/**
 * Checks the live sidecar's /health within the 3.0 second limit. If it fails, restores
 * the newest snapshot and restarts the sidecar, then shows a banner saying what happened.
 */
async function verifyAndRecover(): Promise<void> {
  if (!isTauriRuntime() || reloading || recovering || streaming) {
    return;
  }
  recovering = true;
  refreshControls();
  try {
    const health = await invoke<HealthReport>("verify_health");
    if (health.ok) {
      return;
    }
    console.warn("Health check failed; rolling back", health.detail);
    setStatus("reloading");
    try {
      const outcome = await invoke<RollbackOutcome>("rollback_latest");
      connection = { port: outcome.port, token: outcome.token };
      setStatus("connected");
      showRollbackNotice(
        `Daisy recovered from a failed health check and rolled back to snapshot ${outcome.snapshotId}.`,
        "recovered",
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setStatus("error", message);
      showRollbackNotice(`Health check failed and rollback did not succeed: ${message}`, "failed");
    }
  } catch (error) {
    console.error("Health verification could not run", error);
  } finally {
    recovering = false;
    refreshControls();
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

reloadButton.addEventListener("click", () => {
  void reloadSidecar();
});

rollbackDismiss.addEventListener("click", () => {
  hideRollbackNotice();
});

toggle.addEventListener("click", () => {
  const collapsed = app.dataset.collapsed !== "true";
  app.dataset.collapsed = collapsed ? "true" : "false";
  toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  toggle.textContent = collapsed ? "Expand" : "Collapse";
});

async function init(): Promise<void> {
  setStatus("connecting");
  try {
    connection = await loadConnection();
  } catch (error) {
    console.error("Failed to load sidecar connection", error);
    connection = null;
  }
  if (!connection) {
    placeholder.textContent =
      "Backend not connected. In browser dev mode, set localStorage key daisy.devConnection to {\"port\":<port>,\"token\":\"<token>\"}.";
    setStatus("offline");
  } else {
    setStatus("connected");
    await verifyAndRecover();
  }
  refreshControls();
  app.dataset.ready = "true";
}

void init();

export {};
