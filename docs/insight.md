# Insight

Insight is tuvl's browser editor for a project. It reads and writes the same files the CLI does, so anything you do in it
is a change to YAML in git, and anything you change in git shows up in it. Specs, tasks, the workflow canvas, runs, models and
settings are in one place.

```bash
tuvl dev --auto-login        # Insight at http://localhost:8885/insight (needs tuvl[standard])
```

Insight is the `tuvl-insight` package; see [Core and Insight](core-and-insight.md) for what it adds to the engine.
It is a development tool: a production server (`tuvl serve app`) has no Insight and no dev routes. On a terminal `tuvl dev` also
starts a [terminal UI](cli.md#servers) that runs the same commands.

## Pages

| Page | What you do there |
|---|---|
| **Specs** | A tile per spec with its status. Open a spec to read it, see its **Findings** (the rules `tuvl lint spec` applies) and **Traceability** (step, agent, tests), approve, send back or reopen it, derive its tasks, and open a task in a panel you can drag and pop out. **Graph** draws the tasks in execution order; a blocked task says why |
| **Ideas** | The numbered, immutable ideas of the project and the specs generated from each. **Upload idea** takes files, a folder, links (a web page, Jira issue, Confluence page or GitHub issue) or pasted text and makes what `tuvl import idea` makes. **Generate a spec with my agent** runs `tuvl generate spec --idea` with the coding agent you selected, after asking for consent the first time |
| **Workflows** | A canvas over the workflow YAML (the YAML toggle shows the file itself). **+ Add step** (or `A`) adds an agent from a palette of presets, including **Retrieve and answer**, which adds a collection search routed into an answering model. Select an agent to edit it in the side panel; run a workflow with breakpoints and step mode, and check a change of engine before applying it |
| **Runs** | Every run with its journal, cost and approvals, live over SSE |
| **Artifacts** | Prompts, steering, skills, guardrails, hooks, MCP servers, judges and decision tables, with their versions and what uses them |
| **Models** | Data models: fields, relations, access and the CRUD operations exposed |
| **AI Models** | The model presets (`llms/`) agents refer to as `model: default`, with provider and key variable |
| **Embeddings**, **Collections** | The vector search setup that `tuvl.data_search`, `tuvl.data_ingest` and a loop's `collection:` tool use. A banner appears when pgvector is not available |
| **Memory** | What agents remember across runs, per collection: the owners, each memory with the agent and run that wrote it, a filter by owner, and **Forget** for one memory or (after a second click) everything of one owner. Reads and deletes through the dev API; `tuvl prune memory` does the same from the command line |
| **Datasources**, **Storage** | PostgreSQL, Redis and blob stores; a store can be checked (write, read back, delete a probe object) and its objects browsed from the page |
| **IAM**, **Federation** | Users, roles, scopes and groups; Google, GitHub, Microsoft or custom sign-in providers |
| **API Docs** | The OpenAPI generated from your workflows' contracts |
| **Audit** | The local audit ledger and, when developer telemetry is on, the **Team** view of what everyone did |
| **Settings** | API access (which routes the production server exposes), developer telemetry, the project's identity and its listing in Nest |

## The agent panel

Selecting an agent on the canvas opens a panel with these tabs:

| Tab | Holds |
|---|---|
| **Engine** (named **Rules**, **Prompt**, **Loop**, **Tool**, **Approval** by engine) | The engine's own settings. An `llm` or `loop` agent also has **Remember across runs**: the memory collection, whose memory (the signed-in user, the tenant or an input), how many recent and related memories, the prompt budget, what is stored and how long it is kept. An `llm` or `loop` agent picks its **AI model** from the configured presets (or types a `provider/model` id), and writes its system prompt, prompt or steering in place or takes it from a stored artifact pinned to a version; skills, MCP servers and collections it may search are listed here too. A `tuvl.data_search` or `tuvl.data_ingest` step picks its **collection** |
| **Contract** | The agent's typed inputs and outputs |
| **Routes** | Where each signal goes |
| **Budget** | Tokens, time and iterations |
| **Probe** | Run this one agent alone, live, with inputs you type (an `llm` or `loop` agent spends real tokens; save the workflow first) |
| **YAML** | The whole agent as it is in the workflow file, editable; its id cannot change here because routes point at it |
| **Governance** | Effect, guardrails, hooks and the compensating agent |

A code agent also has a **Code** tab with the Python of its function.

## What Insight does not do

- It does not hold state of its own: close it and nothing is lost, because everything is in the files.
- It does not run in production, and it cannot see a deployed server's data.
- It spends tokens only where you ask a model or a coding agent to: checking a spec, deriving tasks and linting are free.
