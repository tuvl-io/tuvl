<p align="center">
  <img src="assets/logo.png" alt="" width="40" />&nbsp;&nbsp;<span style="font-size:2em"><strong>tuvl</strong></span><br/>
  <sub>/ˈtuːvəl/ &nbsp;·&nbsp; തൂവൽ &nbsp;·&nbsp; feather</sub>
</p>

<p align="center">
  Lean, YAML-driven workflow orchestration engine designed for deterministic AI execution.<br/>
  Compose complex pipelines of functional nodes, LLM calls, API integrations, and MCP tools —<br/>
  instantly served as highly scalable REST APIs with native Postgres persistence and Redis-backed state management.
</p>

<p align="center">
  <a href="https://tuvl.io">Website</a> &nbsp;·&nbsp;
  <a href="https://tuvl.dev">Docs</a>
</p>

---

> **Hey there!** 👋 The source code is on its way — we're tidying things up before the public release. Keep an eye on this repo and star it so you don't miss the drop.

---

## What is tuvl?

`tuvl` lets you define, run, and manage multi-step AI workflows using plain YAML files. No boilerplate. No complex overhead. No lock-in.

Install `tuvl[standard]` during development to get **Tuvl Insight** — a full browser-based developer portal for designing models, configuring datasources, managing LLM providers, building workflows visually, controlling access, and testing execution — all without leaving your local environment.

---

## 📦 Installation

> **Coming soon** — `tuvl` is not yet published. The PyPI package, npm client, and source code will all be released together. Star this repo to get notified.

### Core (production server)

```bash
pip install tuvl
# or
uv add tuvl
```

### With Tuvl Insight (developer portal)

```bash
pip install "tuvl[standard]"
# or
uv add "tuvl[standard]"
```

Adds **Tuvl Insight** — a full browser-based developer portal, active only when `TUVL_DEV_MODE=true`.

### CLI (global tool)

```bash
uv tool install tuvl
```

### Client SDKs

| SDK | Package | Status |
|-----|---------|--------|
| JavaScript / TypeScript | `npm install @tuvl/client` | Coming soon |

The `@tuvl/client` package provides a typed TypeScript client for triggering workflows, subscribing to execution events via SSE, and managing workflow instances from your own applications.

---

## 🚀 Quick Start

**1. Scaffold a new project:**

```bash
tuvl init my-project
```

Prompts for Postgres and LLM provider credentials and writes a `pyproject.toml` + `.env`.

**2. Install dependencies:**

```bash
uv sync
```

**3. Start the development server:**

```bash
uv run tuvl dev
```

Starts the engine with hot reload on `http://localhost:8000`. If `tuvl[standard]` is installed, the Insight dashboard is available at `http://localhost:8000/insight`.

**4. Run in production:**

```bash
tuvl run --project-dir /path/to/project --workers 4
```

---

## 🗂️ Project Structure

```
my-project/
├── pyproject.toml        # Project deps — add extras with: uv add <pkg>
├── .env                  # LLM API keys and database config (git-ignored)
├── .env.example          # Safe-to-commit template
├── config.yaml           # Directory layout (customisable)
├── models/               # Data model definitions (YAML)
├── datasources/          # Datasource definitions (YAML)
├── llms/                 # LLM / AgentModel configs (YAML)
├── nodes/                # Custom Python node implementations
└── workflows/            # Workflow definitions (YAML)
```

---

## ⚙️ CLI Reference

| Command | Description |
|---|---|
| `tuvl init <name>` | Scaffold a new project |
| `tuvl init <name> --sample` | Scaffold with a sample recruitment pipeline |
| `tuvl dev` | Start the engine in dev mode with hot reload |
| `tuvl run` | Start the production server |
| `tuvl validate` | Validate workflow and model YAML files |

### Common options

```bash
tuvl dev --port 9000 --project-dir ./my-project
tuvl run --host 0.0.0.0 --port 8000 --workers 2
tuvl run --allow-host 10.0.0.0/8
tuvl validate --project-dir ./my-project
```

---

## 🔩 Workflow Step Kinds

Each step in a workflow has a `kind`:

| Kind | Description |
|---|---|
| `functional` | Execute a registered Python node function |
| `agent` | Call an LLM via LiteLLM with a prompt template |
| `api_call` | Make an outbound HTTP request and map the response into context |
| `mcp` | Call a tool via the Model Context Protocol (stdio or SSE) |
| `router` | Evaluate a condition and branch to a named route |
| `model-op` | Perform CRUD operations on a registered data model |
| `response` | Shape and return the final HTTP response |
| `HumanInTheLoop` | Suspend execution for a human reviewer |

---

## 🔬 Tuvl Insight (Developer Portal)

Installed via `pip install "tuvl[standard]"`. Active only when `TUVL_DEV_MODE=true`.

Tuvl Insight is a complete **local developer portal** covering the full development lifecycle:

| Section | Description |
|---|---|
| **Workflows** | Visual YAML editor with a live graph view of step connections and node types |
| **Models** | Form-driven model designer — fields, types, relationships, constraints |
| **Datasources** | Configure Postgres datasource connections |
| **LLM Models** | Manage AgentModel definitions (OpenAI, Anthropic, Ollama, Groq, Gemini, custom) |
| **IAM** | Create and manage users, roles, and scopes |
| **Federation** | Configure OAuth2/OIDC providers (Google, GitHub, Microsoft, custom) |
| **API Docs** | Embedded Swagger UI and ReDoc for the live tuvl REST API |
| **Tuvl Lens** | Unit-test individual workflow steps with mock context |
| **Tuvl Spectrum** | Full execution trace with per-step state snapshots and timing |

---

## 📄 License

MIT

---

## 📬 Contact

Questions, feedback, or just want to say hi? Reach us at [developer@tuvl.io](mailto:developer@tuvl.io)
