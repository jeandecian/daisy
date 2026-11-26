/**
 * Daisy frontend entry point (Vanilla TypeScript).
 * Split-view scaffold: chat pane (left) and artifacts pane (right).
 */

interface ChatMessage {
  role: "user" | "assistant";
  text: string;
}

const messages: ChatMessage[] = [];

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

function renderMessage(msg: ChatMessage): void {
  placeholder.hidden = true;
  const item = document.createElement("div");
  item.className = `chat-message chat-message--${msg.role}`;
  item.textContent = msg.text;
  list.appendChild(item);
  list.scrollTop = list.scrollHeight;
}

function submitMessage(): void {
  const text = input.value.trim();
  if (text === "") {
    return;
  }
  const userMessage: ChatMessage = { role: "user", text };
  messages.push(userMessage);
  renderMessage(userMessage);
  input.value = "";
  autosize();
  sendButton.disabled = true;

  // Placeholder until the backend SSE stream is wired up.
  const reply: ChatMessage = {
    role: "assistant",
    text: "Backend stream not connected yet.",
  };
  messages.push(reply);
  renderMessage(reply);
  sendButton.disabled = false;
}

function autosize(): void {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

form.addEventListener("submit", (event: SubmitEvent) => {
  event.preventDefault();
  submitMessage();
});

input.addEventListener("keydown", (event: KeyboardEvent) => {
  // Enter sends; Shift+Enter inserts a newline.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submitMessage();
  }
});

input.addEventListener("input", autosize);

toggle.addEventListener("click", () => {
  const collapsed = app.dataset.collapsed !== "true";
  app.dataset.collapsed = collapsed ? "true" : "false";
  toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  toggle.textContent = collapsed ? "Expand" : "Collapse";
});

app.dataset.ready = "true";

export {};
