# TUVL Framework — AI Agent Onboarding Guide

Welcome to the **tuvl** repository. You are an AI agent operating within the codebase of `tuvl`, a declarative, YAML-driven workflow orchestration engine built on FastAPI, PostgreSQL, and Redis.

This document serves as your primary reference for understanding the architecture, design philosophy, and strict coding conventions of this project. Read these rules carefully before suggesting or making changes.

## 1. Project Overview & Philosophy
- **Declarative First:** `tuvl` relies heavily on YAML configuration to generate FastAPI routes, PostgreSQL models, and AI workflows dynamically at startup. Always prefer YAML over writing Python boilerplate.
- **No Black Boxes:** The engine executes exactly what is defined in the YAML pipelines.
- **Data-Driven:** Business logic is modeled as a state-machine of operations passing context variables.
- **Strict Scope Guarding:** All data operations enforce role-based access control (RBAC) scopes extracted from cryptographically verified Biscuit tokens.

## 2. Directory Structure & Conventions
The project strictly separates components by their YAML `kind`. When generating files for a `tuvl` project, place them in the correct directories:
- `models/` ➔ `ModelDefinition`, `EmbeddingRegistry`, `CollectionRegistry`
- `workflows/` ➔ `Workflow`
- `datasources/` ➔ `DataSource`
- `llms/` ➔ `AgentModel`
- `federation/` ➔ `FederationProvider`
- `nodes/` ➔ Custom Python implementation (`@node()` decorators)
- `artifacts/` ➔ Named, versioned assets: prose `.md` with YAML front-matter (types `prompt` | `steering` | `skill`) and structured `kind: Artifact` YAML (types `guardrail` | `hook` | `mcp`). Referenced anywhere via `artifact://name[@version]`; pin `@version` for production.

**Never invent fields, step kinds, or document kinds.** Rely only on the specified vocabulary. Every YAML document uses the spec-wrapped envelope (`kind:` + `metadata:` + `spec:`) — the flat root-level form is rejected.

## 3. Workflow Implementation Rules
Workflows are sequences of steps defined in YAML. Triggers receive HTTP request data into a shared `context: dict[str, Any]`.

- **Database Allowlist:** Any model accessed inside a workflow (whether natively via `ModelOp` or in custom Python nodes) **must** be explicitly listed in the workflow's `spec.context.models`. Missing this triggers a `PermissionError`.
- **Reserved Keys:** You are forbidden from mutating core engine context keys including: `_session`, `_db`, `_step`, `_response`, `_last_error`, `_last_error_type`, `_api_status_code`, `_context_model_versions`, `_schema_version`, `_instance_id`, `_user_id`.
- **Routing Strictness:** Every step must return a signal (e.g., `default`, `true`, `false`, `error`). You must define an explicit mapping for every non-default signal in the step's `routes:` map. Unmapped non-default signals will raise a `RuntimeError`.
- **Step Kinds (closed set, 8):** `Functional`, `Agent`, `Router`, `APICall`, `MCP`, `ModelOp`, `Response`, `HumanInTheLoop`. Never invent others; `AutonomousAgent` no longer exists.
  - `Agent` is the one LLM step and **REQUIRES `mode: completion | autonomous` at the step level** (no default — the validator errors and the runtime raises). `mode: completion` is a single retried LLM call (fields `system` / `prompt`); `mode: autonomous` is a bounded tool-calling loop (fields `steering` / `tools` (REQUIRED) / `max_iterations` / `token_budget`) where the model picks tools (each `agent.tools[].ref` names another step in the workflow), observes results, and re-decides until it emits one of a declared `outcome.enum`. Each tool's description (REQUIRED) is sourced from the referenced step's top-level `description:`, overridable by `agent.tools[].description`.
    - **Outcome contract (both modes):** `agent.outcome: {write, format: json|text, enum, map}`. `write` is the context key receiving the result (default `<step_id>_result`). With `enum` declared the model must return an `"outcome"` field holding exactly one declared value — that is the route signal; map every enum value plus the applicable reserved exits (`error`, `parse_error` / `timeout` in completion, `max_iterations` / `budget_exceeded` / `aborted` in autonomous, `guardrail_violation` when guardrails attach) in `routes:`. The old `output.{format,map,signal_from}` and `outcome.output_key` are gone.
    - **Steering & skills:** `agent.steering` is the persistent instruction, ALWAYS injected; `agent.skills` are injected when relevant. Both (and `system` / `prompt`) take inline text or `artifact://` references — `steering_files` and the `agents/<workflow>__<stepId>/` directory scheme are gone.
    - **Guardrails & hooks:** `agent.guardrails: {input|output|tools: [artifact://…]}` attach `type: guardrail` artifacts (closed checks: `json_schema`, `regex_deny`, `max_chars`, `pii_mask`, `llm_judge`); a failing check routes the reserved `guardrail_violation` signal. Observe-only `type: hook` artifacts attach per step (`hooks:`) or workflow-wide (`spec.hooks:`) and never affect flow.
    - **Supervisor:** an optional per-workflow `spec.supervisor` block (a sibling of `steps:`, NOT a step) watches autonomous `Agent` runs live and can pause / abort / steer them via deterministic `rules` and/or an LLM judge. `criteria` is inline text or an `artifact://` ref to a `steering` artifact (`criteria_file` was removed; `steer_message` sets the rules-path steer text). `abort` exits through the reserved `"aborted"` signal. Operators observe/control at `/api/agents/runs` (scopes `agent:observe` / `agent:control`) or the Insight Agents dashboard.
  - `MCP` steps declare `mcp.server: artifact://<name>` pointing at a `type: mcp` artifact that owns the connection config (transport / url / headers / command / args / env) — inline transport blocks on the step are rejected.
  - `Router` supports a multi-way `match:` switch (`match: { field: user.country }`) for data-driven branching — keep deterministic routing here, never inside an agent.

## 4. Custom Python Nodes (`Functional` steps)
When YAML is insufficient, you can create a custom `Functional` Python node.
- **One Node Per File:** You **MUST** put only one `@node()` decorator per Python file.
- **Filename Matching:** The Python filename must strictly match the node runner name. For example, a node decorated with `@node("score_resume")` **must** be placed in `nodes/score_resume.py`.

## 5. PostgreSQL, Models, and Tenancy
- Every `ModelDefinition` generates a Postgres table and auto-generates a full suite of CRUD REST endpoints.
- **Multi-Tenancy:** The engine ships single-tenant by default, but can operate in multi-tenant mode via Postgres RLS (Row-Level Security). Do not emit `tenant_id` fields manually unless explicitly instructed.
- **Security:** Fields containing PII or sensitive data must be marked with `secure: true` in their YAML definition.

## 6. Security & Identity
- **Biscuit Tokens:** The engine relies on offline-verifiable Biscuit tokens for authentication.
- **Dev Sentinel:** During local development (`tuvl dev`), a dev sentinel keypair is used. Production refuses to boot if this sentinel is active.
- **CRUD Scopes:** Auto-generated CRUD routes enforce Biscuit scopes (e.g., `{modelname.lower()}:read`).

## 7. Developer Tooling
If asked to test or run the project locally, use the CLI commands:
- `tuvl dev` (or `uv run tuvl dev`): Starts the engine with hot-reloading and mounts the Tuvl Insight Developer Portal at `http://localhost:8000/insight`.
- `tuvl dev --auto-login`: Bypasses the local security screen for rapid API testing.
- `tuvl run`: Starts the highly-optimized production Uvicorn server without hot-reload.
- `tuvl validate`: Validates every YAML config, node, and cross-reference without starting the server.
- `tuvl ship`: Packages the project for production — validates it, generates a production `Dockerfile` and a Helm chart under `deploy/chart/<name>/`, then builds the container image (`--no-build` to skip the build, `--push` to publish).

> **Agentic Note:** For complete syntax schemas and deep reference, consult `docs/tuvl-agentic-manual.md`. Always write minimal, functional code that conforms to these architectural invariants.
