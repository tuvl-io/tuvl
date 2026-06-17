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

**Never invent fields, step kinds, or document kinds.** Rely only on the specified vocabulary.

## 3. Workflow Implementation Rules
Workflows are sequences of steps defined in YAML. Triggers receive HTTP request data into a shared `context: dict[str, Any]`.

- **Database Allowlist:** Any model accessed inside a workflow (whether natively via `model-op` or in custom Python nodes) **must** be explicitly listed in the workflow's `spec.context.models`. Missing this triggers a `PermissionError`.
- **Reserved Keys:** You are forbidden from mutating core engine context keys including: `_session`, `_db`, `_step`, `_response`, `_last_error`, `_last_error_type`, `_api_status_code`, `_context_model_versions`, `_schema_version`, `_instance_id`, `_user_id`.
- **Routing Strictness:** Every step must return a signal (e.g., `default`, `true`, `false`, `error`). You must define an explicit mapping for every non-default signal in the step's `routes:` map. Unmapped non-default signals will raise a `RuntimeError`.

## 4. Custom Python Nodes (`functional` steps)
When YAML is insufficient, you can create a custom `functional` Python node.
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

> **Agentic Note:** For complete syntax schemas and deep reference, consult `KNOWME.md` and `docs/TUVL_AGENTIC_MANUAL.md`. Always write minimal, functional code that conforms to these architectural invariants.
