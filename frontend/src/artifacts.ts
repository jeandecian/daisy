/**
 * Artifacts panel: staged proposals, line diffs, pre-flight badges, LLM judge verdicts and HITL controls.
 *
 * The sidecar is reached only through Tauri commands (propose_proposal, judge_proposal,
 * approve_proposal, reject_proposal, apply_proposal). Nothing is written until the user clicks
 * Approve & Apply, and approval is recorded under APPROVER_NAME before the apply command runs.
 * The judge verdict is advisory: it is shown to the reviewer but never approves or blocks anything.
 */

import { invoke } from "@tauri-apps/api/core";

interface CheckPayload {
  name: string;
  passed: boolean;
  errors: string[];
  warnings: string[];
  stdout: string;
  stderr: string;
  returncode: number | null;
}

interface PreflightPayload {
  passed: boolean;
  checks: CheckPayload[];
}

/** Structured audit result from POST /proposals/{id}/judge. */
export interface JudgeVerdict {
  verdict: string;
  confidence: number;
  risks: string[];
  summary: string;
  provider?: string;
}

interface ProposalPayload {
  proposal_id: string;
  status: string;
  created_at: string;
  paths: string[];
  decided_by: string | null;
  snapshot_id: string | null;
  error: string | null;
  preflight: PreflightPayload;
  /** Null or absent until the judge has run on this proposal. */
  judge_verdict?: JudgeVerdict | null;
}

/** Before and after text for one file. `before` is "" for a file that does not exist yet. */
export interface FileChange {
  before: string;
  after: string;
}

interface ProposalView {
  payload: ProposalPayload;
  files: Record<string, FileChange>;
  busy: boolean;
}

type NoticeKind = "info" | "success" | "error";

interface Notice {
  kind: NoticeKind;
  text: string;
}

type DiffOp = { kind: "add" | "del" | "ctx"; text: string };

export const APPROVER_NAME = "Jean Decian";

const CHECK_ORDER: readonly string[] = ["ruff_check", "ruff_format", "mypy"];
const CHECK_LABELS: Record<string, string> = {
  ruff_check: "Ruff Check",
  ruff_format: "Ruff Format",
  mypy: "Mypy",
};
const STATUS_LABELS: Record<string, string> = {
  pending: "Awaiting approval",
  approved: "Approved",
  rejected: "Rejected",
  applied: "Applied",
  failed: "Failed",
};
const VERDICT_LABELS: Record<string, string> = {
  APPROVED: "Approved",
  WARNING: "Warning",
  REJECTED: "Rejected",
};
const DIFF_SIGN: Record<DiffOp["kind"], string> = { add: "+", del: "-", ctx: " " };
const MAX_NOTICES = 5;
const MAX_ERROR_LINES = 8;
const MAX_RISK_LINES = 12;
/** Caps the LCS table (rows x columns) so a very large file cannot freeze the panel. */
const MAX_DIFF_CELLS = 1_000_000;

const views = new Map<string, ProposalView>();
let notices: Notice[] = [];

function requireEl<T extends HTMLElement>(selector: string): T {
  const found = document.querySelector<T>(selector);
  if (!found) {
    throw new Error(`Missing required element: ${selector}`);
  }
  return found;
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function errorText(error: unknown): string {
  if (typeof error === "string") {
    return error;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

const content = requireEl<HTMLElement>("#artifact-content");
const noticeRegion = el("div", "artifact-notices");
noticeRegion.setAttribute("role", "status");
noticeRegion.setAttribute("aria-live", "polite");
const cardRegion = el("div", "artifact-cards");
content.replaceChildren(noticeRegion, cardRegion);

function pushNotice(kind: NoticeKind, text: string): void {
  notices = [{ kind, text }, ...notices].slice(0, MAX_NOTICES);
  render();
}

/** Splits text into lines, dropping the empty entry produced by a trailing newline. */
function splitLines(text: string): string[] {
  if (text === "") {
    return [];
  }
  const lines = text.split("\n");
  if (lines[lines.length - 1] === "") {
    lines.pop();
  }
  return lines;
}

/** Line diff via longest common subsequence. Returns null when the inputs are too large. */
function diffLines(before: string, after: string): DiffOp[] | null {
  const a = splitLines(before);
  const b = splitLines(after);
  const n = a.length;
  const m = b.length;
  if ((n + 1) * (m + 1) > MAX_DIFF_CELLS) {
    return null;
  }
  const width = m + 1;
  const table = new Uint32Array((n + 1) * (m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i * width + j] =
        (a[i] ?? "") === (b[j] ?? "")
          ? (table[(i + 1) * width + j + 1] ?? 0) + 1
          : Math.max(table[(i + 1) * width + j] ?? 0, table[i * width + j + 1] ?? 0);
    }
  }
  const ops: DiffOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    const left = a[i] ?? "";
    const right = b[j] ?? "";
    if (left === right) {
      ops.push({ kind: "ctx", text: left });
      i++;
      j++;
    } else if ((table[(i + 1) * width + j] ?? 0) >= (table[i * width + j + 1] ?? 0)) {
      ops.push({ kind: "del", text: left });
      i++;
    } else {
      ops.push({ kind: "add", text: right });
      j++;
    }
  }
  while (i < n) {
    ops.push({ kind: "del", text: a[i] ?? "" });
    i++;
  }
  while (j < m) {
    ops.push({ kind: "add", text: b[j] ?? "" });
    j++;
  }
  return ops;
}

function firstLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trimEnd())
    .filter((line) => line.trim() !== "");
}

function confidencePercent(confidence: number): number {
  const clamped = Math.min(Math.max(confidence, 0), 1);
  return Math.round(clamped * 100);
}

function renderNotice(notice: Notice): HTMLElement {
  return el("div", `artifact-notice artifact-notice--${notice.kind}`, notice.text);
}

function renderPaths(paths: string[]): HTMLElement {
  const list = el("ul", "artifact-paths");
  for (const path of paths) {
    const item = el("li");
    item.appendChild(el("code", undefined, path));
    list.appendChild(item);
  }
  return list;
}

function renderPreflight(preflight: PreflightPayload): HTMLElement {
  const section = el("section", "preflight");
  section.setAttribute("aria-label", "Pre-flight checks");
  const header = el("div", "preflight-header");
  header.appendChild(el("h4", "artifact-subtitle", "Pre-flight checks"));
  const summary = el(
    "span",
    "preflight-summary",
    preflight.checks.length === 0
      ? "Not run"
      : preflight.passed
        ? "All checks passed"
        : "Checks failed",
  );
  summary.dataset.state = preflight.checks.length === 0 ? "na" : preflight.passed ? "passed" : "failed";
  header.appendChild(summary);
  section.appendChild(header);

  if (preflight.checks.length === 0) {
    section.appendChild(el("p", "artifact-meta", "No Python files in this change, so checks were not run."));
    return section;
  }

  const byName = new Map(preflight.checks.map((check) => [check.name, check] as const));
  const badges = el("div", "preflight-badges");
  for (const name of CHECK_ORDER) {
    const check = byName.get(name);
    const state = check === undefined ? "na" : check.passed ? "passed" : "failed";
    const label = CHECK_LABELS[name] ?? name;
    const badge = el("span", "preflight-badge", `${label}: ${state === "na" ? "not run" : state}`);
    badge.dataset.state = state;
    badges.appendChild(badge);
  }
  section.appendChild(badges);

  for (const name of CHECK_ORDER) {
    const check = byName.get(name);
    if (!check || check.passed) {
      continue;
    }
    const lines = check.errors.length > 0 ? check.errors : firstLines(check.stderr || check.stdout);
    const block = el("div", "preflight-errors");
    block.appendChild(el("p", "artifact-meta", `${CHECK_LABELS[name] ?? name} output`));
    const list = el("ul");
    for (const line of lines.slice(0, MAX_ERROR_LINES)) {
      list.appendChild(el("li", undefined, line));
    }
    if (lines.length > MAX_ERROR_LINES) {
      list.appendChild(el("li", "artifact-meta", `and ${lines.length - MAX_ERROR_LINES} more`));
    }
    block.appendChild(list);
    section.appendChild(block);
  }
  return section;
}

/** Judge card: verdict badge, confidence, summary, risks, and a Re-Judge control. */
function renderJudge(id: string, view: ProposalView): HTMLElement {
  const { payload, busy } = view;
  const verdict = payload.judge_verdict ?? null;
  const card = el("section", "judge-card");
  card.setAttribute("aria-label", "LLM judge verdict");

  const header = el("div", "judge-header");
  header.appendChild(el("h4", "artifact-subtitle", "LLM judge"));
  if (verdict) {
    const badge = el(
      "span",
      "judge-badge",
      VERDICT_LABELS[verdict.verdict] ?? verdict.verdict,
    );
    badge.dataset.verdict = verdict.verdict.toLowerCase();
    header.appendChild(badge);
  }
  card.appendChild(header);

  if (!verdict) {
    card.appendChild(el("p", "artifact-meta", "Not audited yet. Run Re-Judge to get a verdict."));
  } else {
    const meta = el(
      "p",
      "artifact-meta",
      `${confidencePercent(verdict.confidence)}% confidence${verdict.provider ? ` · ${verdict.provider}` : ""}`,
    );
    card.appendChild(meta);

    if (verdict.verdict === "REJECTED") {
      const warning = el(
        "p",
        "judge-warning",
        "The LLM judge rejected this change. Read the risks below before you approve.",
      );
      warning.setAttribute("role", "alert");
      card.appendChild(warning);
    }

    if (verdict.summary) {
      card.appendChild(el("p", "judge-summary", verdict.summary));
    }

    if (verdict.risks.length > 0) {
      const section = el("div", "judge-risks");
      section.appendChild(el("p", "artifact-meta", "Risks"));
      const list = el("ul");
      for (const risk of verdict.risks.slice(0, MAX_RISK_LINES)) {
        list.appendChild(el("li", undefined, risk));
      }
      if (verdict.risks.length > MAX_RISK_LINES) {
        list.appendChild(el("li", "artifact-meta", `and ${verdict.risks.length - MAX_RISK_LINES} more`));
      }
      section.appendChild(list);
      card.appendChild(section);
    }
  }

  const actions = el("div", "judge-actions");
  const rejudge = el("button", "artifact-button", "Re-Judge");
  rejudge.type = "button";
  rejudge.disabled = busy || (payload.status !== "pending" && payload.status !== "approved");
  rejudge.addEventListener("click", () => {
    void rejudgeProposal(id);
  });
  actions.appendChild(rejudge);
  card.appendChild(actions);
  return card;
}

function renderDiff(path: string, change: FileChange): HTMLElement {
  const section = el("section", "artifact-diff-block");
  const ops = diffLines(change.before, change.after);
  const removed = ops
    ? ops.filter((op) => op.kind === "del").length
    : splitLines(change.before).length;
  const added = ops ? ops.filter((op) => op.kind === "add").length : splitLines(change.after).length;

  const header = el("div", "artifact-diff-header");
  header.appendChild(el("h4", "artifact-subtitle", path));
  header.appendChild(el("span", "artifact-diff-counts", `+${added} -${removed}`));
  section.appendChild(header);

  const view = el("div", "artifact-diff");
  view.setAttribute("role", "group");
  view.setAttribute("aria-label", `Changes to ${path}`);
  let lines: DiffOp[];
  if (ops) {
    lines = ops;
  } else {
    view.appendChild(
      el("p", "artifact-meta", "File too large for an inline diff; showing the full proposed content."),
    );
    lines = splitLines(change.after).map((text) => ({ kind: "add" as const, text }));
  }
  for (const op of lines) {
    const row = el("div", "diff-line", `${DIFF_SIGN[op.kind]} ${op.text}`);
    row.dataset.kind = op.kind;
    view.appendChild(row);
  }
  section.appendChild(view);
  return section;
}

function renderActions(id: string, view: ProposalView): HTMLElement {
  const { payload, busy } = view;
  const row = el("div", "artifact-actions");

  const canApprove =
    payload.status === "approved" || (payload.status === "pending" && payload.preflight.passed);
  const approve = el(
    "button",
    "artifact-button artifact-button--primary",
    payload.status === "approved" ? "Apply" : "Approve & Apply",
  );
  approve.type = "button";
  approve.disabled = busy || !canApprove;
  approve.addEventListener("click", () => {
    void approveAndApply(id);
  });
  row.appendChild(approve);

  const reject = el("button", "artifact-button", "Reject");
  reject.type = "button";
  reject.disabled = busy || payload.status !== "pending";
  reject.addEventListener("click", () => {
    void rejectProposal(id);
  });
  row.appendChild(reject);

  if (payload.status === "pending" && !payload.preflight.passed) {
    row.appendChild(el("p", "artifact-meta", "Fix the pre-flight errors before approving."));
  }
  if (payload.status === "pending" && payload.judge_verdict?.verdict === "REJECTED") {
    row.appendChild(
      el("p", "judge-warning", "Approving asks for confirmation because the LLM judge rejected this change."),
    );
  }
  return row;
}

function renderProposal(id: string, view: ProposalView): HTMLElement {
  const { payload, files } = view;
  const card = el("article", "artifact-card");
  card.dataset.proposalId = id;
  card.dataset.status = payload.status;
  card.setAttribute("aria-label", "Proposed change");

  const header = el("div", "artifact-card-header");
  header.appendChild(el("span", "artifact-card-title", "Proposed change"));
  header.appendChild(el("span", "artifact-status", STATUS_LABELS[payload.status] ?? payload.status));
  card.appendChild(header);

  const body = el("div", "artifact-card-section");
  body.appendChild(renderPaths(payload.paths));
  body.appendChild(renderPreflight(payload.preflight));
  body.appendChild(renderJudge(id, view));
  for (const path of payload.paths) {
    const change = files[path];
    if (change) {
      body.appendChild(renderDiff(path, change));
    }
  }
  if (payload.snapshot_id) {
    body.appendChild(el("p", "artifact-meta", `Snapshot: ${payload.snapshot_id}`));
  }
  if (payload.decided_by) {
    body.appendChild(el("p", "artifact-meta", `Decided by ${payload.decided_by}`));
  }
  if (payload.error) {
    body.appendChild(el("p", "artifact-error", payload.error));
  }
  if (payload.status === "pending" || payload.status === "approved") {
    body.appendChild(renderActions(id, view));
  }
  card.appendChild(body);
  return card;
}

function render(): void {
  noticeRegion.replaceChildren(...notices.map(renderNotice));
  cardRegion.replaceChildren();
  if (views.size === 0) {
    cardRegion.appendChild(
      el("p", "artifact-empty", "No artifacts yet. Diffs and file previews will appear here."),
    );
    return;
  }
  for (const [id, view] of views) {
    cardRegion.appendChild(renderProposal(id, view));
  }
}

function setBusy(view: ProposalView, busy: boolean): void {
  view.busy = busy;
  render();
}

/**
 * Records APPROVER_NAME's approval, then applies the proposal. If approval already happened
 * (status approved), only the apply runs, so a failed apply can be retried. When the judge
 * rejected the change, the reviewer must confirm before approval is recorded.
 */
async function approveAndApply(id: string): Promise<void> {
  const view = views.get(id);
  if (!view || view.busy) {
    return;
  }
  if (view.payload.status === "pending" && view.payload.judge_verdict?.verdict === "REJECTED") {
    const confirmed = window.confirm(
      "The LLM judge rejected this change. Approve and apply it anyway?",
    );
    if (!confirmed) {
      return;
    }
  }
  setBusy(view, true);
  try {
    if (view.payload.status === "pending") {
      view.payload = await invoke<ProposalPayload>("approve_proposal", {
        proposalId: id,
        approver: APPROVER_NAME,
      });
    }
    view.payload = await invoke<ProposalPayload>("apply_proposal", { proposalId: id });
    pushNotice(
      "success",
      `Applied ${view.payload.paths.join(", ")}. Snapshot ${view.payload.snapshot_id ?? "not recorded"}.`,
    );
  } catch (error) {
    pushNotice("error", `Could not apply: ${errorText(error)}`);
  } finally {
    setBusy(view, false);
  }
}

async function rejectProposal(id: string): Promise<void> {
  const view = views.get(id);
  if (!view || view.busy) {
    return;
  }
  setBusy(view, true);
  try {
    view.payload = await invoke<ProposalPayload>("reject_proposal", {
      proposalId: id,
      approver: APPROVER_NAME,
    });
    pushNotice("info", `Rejected ${view.payload.paths.join(", ")}. Nothing was written.`);
  } catch (error) {
    pushNotice("error", `Could not reject: ${errorText(error)}`);
  } finally {
    setBusy(view, false);
  }
}

/** Re-runs the LLM judge. The verdict is advisory; nothing is written and approval is unchanged. */
async function rejudgeProposal(id: string): Promise<void> {
  const view = views.get(id);
  if (!view || view.busy) {
    return;
  }
  setBusy(view, true);
  try {
    view.payload = await invoke<ProposalPayload>("judge_proposal", { proposalId: id });
    const verdict = view.payload.judge_verdict;
    if (verdict) {
      pushNotice(
        "info",
        `Judge verdict for ${view.payload.paths.join(", ")}: ${verdict.verdict} (${confidencePercent(verdict.confidence)}% confidence).`,
      );
    } else {
      pushNotice("error", "The judge returned no verdict.");
    }
  } catch (error) {
    pushNotice("error", `Could not run the judge: ${errorText(error)}`);
  } finally {
    setBusy(view, false);
  }
}

/**
 * Stages a change set for review. `before` is used only for the diff; the backend receives
 * `after` and runs pre-flight checks on it. Nothing is written by this call.
 */
export async function stageProposal(files: Record<string, FileChange>): Promise<string | null> {
  const after: Record<string, string> = {};
  for (const [path, change] of Object.entries(files)) {
    after[path] = change.after;
  }
  try {
    const payload = await invoke<ProposalPayload>("propose_proposal", { changes: after });
    views.set(payload.proposal_id, { payload, files, busy: false });
    pushNotice(
      "info",
      `Staged ${payload.paths.join(", ")}. Pre-flight ${payload.preflight.passed ? "passed" : "failed"}.`,
    );
    return payload.proposal_id;
  } catch (error) {
    pushNotice("error", `Could not stage the change: ${errorText(error)}`);
    return null;
  }
}

Object.assign(window, { daisyArtifacts: { stageProposal, rejudge: rejudgeProposal } });

render();
