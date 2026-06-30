# tuvl 2026.2.4

**The first stable release.** tuvl is out of beta.

tuvl turns plain YAML into production-ready, AI-powered FastAPI backends. Declare
your data models and multi-step workflows as configuration; tuvl validates them
at load time and mounts them as fast, auditable HTTP endpoints — local-first,
with zero lock-in and no backend boilerplate.

> The earlier `2026.2.3` / `2026.2.4b1` betas have been withdrawn from PyPI and
> npm and removed from the repository. `2026.2.4` is the first supported
> release and the baseline going forward.

---

## Install

```bash
# CLI
uv tool install tuvl

# or into a project
uv add tuvl              # engine only
uv add "tuvl[standard]"  # + Tuvl Insight developer portal (dev mode)
```

Scaffold and run your first workflow:

```bash
tuvl init my-app --sample
cd my-app && uv sync
uv run tuvl dev          # http://localhost:8000  ·  Insight at /insight
```

TypeScript SDK:

```bash
npm install @tuvl/client
```

---

## ⚠️ One breaking change: step `kind` names are now PascalCase

Workflow step kinds now use one consistent **PascalCase** convention, matching
the document kinds (`Workflow`, `ModelDefinition`, …). Old names are rejected at
load time.

| Old `kind` | New `kind` |
|---|---|
| `functional` | `Functional` |
| `agent` | `Agent` |
| `router` | `Router` |
| `api_call` | `APICall` |
| `mcp` | `MCP` |
| `model-op` | `ModelOp` |
| `response` | `Response` |

`HumanInTheLoop` and `AutonomousAgent` are unchanged.

**To migrate:** change the `kind:` value of each step in `workflows/*.yaml`.
Step config keys (`agent:`, `http:`, `routes:`, `operation:`, …) are unchanged —
only the `kind:` value. Run `tuvl validate` to catch any you miss.

```diff
  steps:
    - id: classify
-     kind: agent
+     kind: Agent
    - id: save
-     kind: model-op
+     kind: ModelOp
```

---

## Highlights

### Eight composable building blocks
A finite, closed set of step kinds — `Functional`, `Agent`, `Router`,
`APICall`, `MCP`, `ModelOp`, `Response`, `HumanInTheLoop` — validated by
Pydantic at load time, so any LLM can target the contract on the first try.

### Autonomous agents
The new **`AutonomousAgent`** step runs a bounded ReAct tool-loop: the model
calls author-declared tools (other steps in the workflow), observes results, and
re-decides until it emits a declared outcome — capped by `max_iterations` /
`token_budget`. Autonomy stays inside the workflow contract. Built on LiteLLM
native tool-calling, no new dependency.

### Data-driven routing
`Router` gains a multi-way **`match:` switch** that branches on a context
field's value — keep deterministic routing out of the LLM.

### Tuvl Insight — local developer portal
Visual workflow editor, model/datasource/LLM designers, IAM, **Lens**
(single-node runs) and **Spectrum** (full execution traces), plus the new
in-editor **Insight AI Chat** assistant. Dev-mode-only — zero surface added to a
deployed engine.

### Secure & observable by default
Biscuit token auth with per-endpoint scopes (fails closed in production), PII
masking on every streamed event, gRPC token-expiry parity with REST,
multi-tenant Postgres RLS tooling, and native OpenTelemetry spans across
requests, steps, LLM calls, and the agent loop.

### Typed SDK
`@tuvl/client` ships REST / SSE / gRPC-Web transports with auto-selection, HITL
resume, typed CRUD, and `agentProgress` for live `AutonomousAgent` loop frames.

---

## Versions

| Surface | Package | Version |
|---|---|---|
| Engine | `tuvl` | `2026.2.4` |
| Developer portal | `tuvl-insight` | `2026.2.4` |
| TypeScript SDK | `@tuvl/client` | `2026.2.4` |
| Docs | `tuvl.dev` | `2026.2.4` |
| Site | `tuvl.io` | `v2026.2.4` |

**Requirements:** Python ≥ 3.13, Postgres 16+ recommended.

---

## Links

- 📚 Docs — https://tuvl.dev
- 🌐 Site — https://tuvl.io
- 💻 Source — https://github.com/tuvl-io/tuvl
- 📦 Full changelog — [`CHANGELOG.md`](./CHANGELOG.md)

— Sooraj Rajagopalan, tuvl.io
