# AGENTS.md — Daisy Engineering & Multi-Agent Constitution

This document defines the normative engineering standards, architectural invariants, and multi-agent collaboration protocols for **Daisy** (a local-first, macOS-first, cross-platform personal AI assistant). Both human contributors and autonomous agents (including Daisy during self-extension) must adhere strictly to these rules.

## 1. Repository & Boundary Map

Agents must keep changes scoped to their respective architectural boundaries:

* `src-tauri/` — Rust native shell, Python sidecar lifecycle supervisor, `3s` boot healthcheck & auto-rollback logic, and OS keychain bridge (`keyring-rs` / `tauri-plugin-stronghold`).

* `ui/` — Lightweight desktop frontend, SSE stream consumer, Split-View Artifacts diff renderer, and Human-in-the-Loop (HITL) confirmation prompts.

* `backend/` — Python orchestration engine, sequential agent runner, Pydantic tool registry, SQLite persistence, and lightweight retrieval (SQLite FTS / Markdown / APIs).

* `extensions/` — User-requested and self-generated plugins/tools subject to pre-flight sandbox checks and `backup/` snapshotting.

* `tests/` — Pytest test suites mirroring `backend/` and `extensions/`.

## 2. Operating Principles & Git Workflow

* **No Direct Pushes to Protected Main:** All changes must occur on short-lived branches (`feat/<topic>`, `fix/<topic>`, `docs/<topic>`, `refactor/<topic>`). Direct commits to `main` are prohibited.

* **Conventional Commits:** Every commit message must follow `type(scope): concise imperative description` (e.g., `feat(ipc): enforce bearer token auth on ephemeral port`, `fix(rollback): restore backup snapshot on sidecar timeout`).

  * Valid types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `sec`.

  * Valid scopes: `core`, `backend`, `ui`, `tauri`, `tools`, `extensions`, `docs`.

* **Definition of Done (DoD):** A task is incomplete until all of the following pass:

  1. Code compiles and passes linting/formatting (`uv run ruff check .` and `uv run ruff format --check .`).

  2. Strict static type checks pass (`uv run mypy backend/ extensions/`).

  3. All unit and integration tests pass (`uv run pytest`).

  4. If `src-tauri/` is modified, Rust checks pass (`cargo check` and `cargo clippy --manifest-path src-tauri/Cargo.toml`).

  5. Zero plaintext secrets, API keys, or tokens are written to disk or committed.

## 3. SOLID & Modularity Standards

Agents must apply SOLID principles pragmatically to keep Daisy extensible without over-engineering:

* **Single Responsibility (SRP):** Separate transport (HTTP/SSE), agent orchestration, tool execution, persistence (SQLite), and UI rendering. Never mix database writes or direct OS calls into SSE stream handlers or Pydantic schema definitions.

* **Open/Closed (OCP):** New tools, BYOK providers, lightweight retrievers, and `extensions/` plugins must register through unified registries or protocols without modifying core orchestration loops or sidecar lifecycle supervisors.

* **Liskov Substitution (LSP):** Interchangeable adapters (e.g., Ollama vs. BYOK inference clients, SQLite FTS vs. API retrievers) must honor identical return contracts, streaming semantics, and error/approval invariants so callers never require provider-specific branches.

* **Interface Segregation (ISP):** Keep Python `Protocol` definitions and Pydantic models narrow and purpose-built. Do not force read-only tools or lightweight retrievers to implement mutating or lifecycle hooks they do not use.

* **Dependency Inversion (DIP):** Core agent loops and orchestration services must depend on narrow `typing.Protocol` interfaces (for inference, secret injection, and retrieval) rather than concrete vendor SDKs or hardcoded filesystem paths.

* **No Speculative Abstractions:** Do not introduce abstract base classes, factories, or indirection layers for a single implementation. Extract a `Protocol` or interface only when there are at least two concrete implementations or an explicit extension boundary (`tools`, `inference`, `retrieval`, `extensions/`).

## 4. Python Engineering Standards

* **Runtime & Tooling:** Python `>= 3.12`, managed exclusively via `uv` (`uv add`, `uv run`, `uv sync`). Never invoke bare `pip` or system `python`.

* **Data Validation & Tool Schemas:** Use **Pydantic v2** (`BaseModel`, `Field`, `ConfigDict`) for all API payloads, SSE event envelopes, configuration models, and tool definitions.

* **Strict Typing (PEP 484 / PEP 604):**

  * All function signatures (parameters and return types) must be explicitly annotated.

  * Use modern union syntax (`str | None`, `int | float`) — never `Optional[...]` or `Union[...]`.

  * Use built-in generic collections (`list[str]`, `dict[str, Any]`) — never `typing.List` or `typing.Dict`.

  * Use timezone-aware UTC timestamps exclusively via `datetime.now(datetime.UTC)`.

* **Filesystem Operations:** Always use `pathlib.Path` for path manipulation and file I/O. Never use `os.path` or raw string concatenation for paths.

* **Async I/O Discipline:** Because the Python sidecar streams Server-Sent Events (SSE) to Tauri, all network and database I/O in the runtime path must be non-blocking (`httpx.AsyncClient`, `aiosqlite`, `anyio`).

* **File Size Budget:**

  * **Target:** $\le 15,000$ characters per source file.

  * **Hard Cap:** $20,000$ characters per source file. Split cohesive modules before exceeding this limit.

## 5. Architectural Invariants (Never Violate)

1. **No Hardcoded Ports:** Never bind the Python sidecar to a static port (such as `8000` or `8080`). Always bind to `127.0.0.1:0` and emit `READY:<assigned_port>` via `stdout` to Tauri.

2. **Mandatory IPC Authentication:** Every HTTP and SSE endpoint exposed by the Python sidecar must validate the ephemeral `Authorization: Bearer <token>` passed via `--auth-token` at startup.

3. **Zero Plaintext Secret Persistence:** Never write BYOK API keys to SQLite, `.env`, JSON, or log files. Keys must reside exclusively in the OS keychain (Apple Keychain on macOS, `libsecret` on Linux, Credential Manager on Windows) and be injected in-memory per request.

4. **Human-in-the-Loop (HITL) Governance:** Any tool registered in the Pydantic tool registry that mutates state or triggers external side effects (`write_file`, `apply_diff`, shell commands, `n8n` webhooks, self-reload triggers) must declare `requires_approval: bool = True`. Only strictly read-only operations may execute automatically.

5. **Safe Self-Extension & Auto-Rollback:** Any modification to `extensions/` must pass sandboxed pre-flight checks (`ruff` + `mypy` + `pytest`) and create a `backup/` snapshot before atomic replacement. Tauri must enforce a `3s` boot healthcheck on sidecar restart and automatically roll back from `backup/` on timeout or non-zero exit.

6. **Lightweight Knowledge First:** Retrieval implementations must prioritize SQLite FTS, local Markdown files, and direct API calls before introducing heavy vector index dependencies.

## 6. Multi-Agent Collaboration Protocol

Agents operate sequentially within a shared, bounded thread. Each role has strict responsibilities and handoff contracts:

1. **User Agent (Intent & Context Scoping):**

   * Parses user intent, identifies the affected boundary (`backend/`, `ui/`, `src-tauri/`, `extensions/`), and reads only the target files and direct dependencies (respecting the $15\text{k}$–$20\text{k}$ char file budget).

   * Outputs a concise task specification with explicit acceptance criteria and target file paths.

2. **Developer Agent (Minimal Diffs & Verification):**

   * Produces minimal, atomic diffs (`apply_diff` / `write_file`) strictly scoped to the task specification.

   * Executes the verification suite (`uv run ruff check .`, `uv run mypy backend/`, `uv run pytest`) and attaches exact command outputs to the handoff.

3. **Reviewer / Judge Agent (Audit & Gatekeeping):**

   * Audits the diff against the **Definition of Done**, **SOLID & Python Standards**, and **Architectural Invariants**.

   * Must never rewrite code directly. Outputs either `APPROVE` (ready for HITL confirmation / merge) or `REJECT` with line-specific citations and actionable remediation steps returned to the Developer Agent.

## 7. Pull Request Template

All pull requests (and major multi-file agent artifacts) must include the following structure:

```markdown
## Description
<!-- Concise summary of the changes, motivation, and architectural context -->

## Scope
<!-- Check all that apply -->
- [ ] Core (`src-tauri/` IPC, lifecycle, keychain)
- [ ] Backend (`backend/` orchestration, agents, tools, retrieval)
- [ ] UI (`ui/` frontend, SSE client, artifacts split-view)
- [ ] Extensions (`extensions/` plugins & custom tools)
- [ ] Docs (`AGENTS.md`, architecture specs)

## Verification
<!-- Exact commands executed and summary of passing output -->
- `uv run ruff check .` -> Passed
- `uv run mypy backend/ extensions/` -> Passed
- `uv run pytest` -> Passed (X passed)
```
