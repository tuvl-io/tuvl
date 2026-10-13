<p align="center">
  <img src="https://raw.githubusercontent.com/tuvl-io/tuvl/main/assets/logo.png" alt="" width="40" />&nbsp;&nbsp;<span style="font-size:2em"><strong>tuvl</strong></span><br/>
  <sub>/ˈtuːvəl/ &nbsp;·&nbsp; തൂവൽ &nbsp;·&nbsp; feather</sub>
</p>

<p align="center">
  Typed workflows of agents — code, database/HTTP/MCP tools, rules, models,<br/>
  bounded tool loops and people — on a durable, journaled runtime,<br/>
  built spec-first and served as REST, SSE and MCP.
</p>

<p align="center">
  <a href="https://tuvl.io">Website</a> &nbsp;·&nbsp;
  <a href="https://tuvl.dev">Docs</a> &nbsp;·&nbsp;
  <a href="https://github.com/tuvl-io/tuvl">GitHub</a>
</p>

> **tuvl 2.0** is a redesign with no 1.x compatibility layer — see the
> [CHANGELOG](CHANGELOG.md). Versions follow [SemVer](https://semver.org).

> [!NOTE]
> **2.0.0 is released on 20 October 2026.** Until then, try the public beta **2.0.0b1** —
> a plain `uv tool install tuvl` still installs 1.x. See [Install](#install).

A tuvl workflow is a graph of **agents**. Each agent has a typed contract — `inputs`, `outputs`, the
signals it emits — and an **engine** that implements it:

| Engine | | Determinism |
|---|---|---|
| `code` | a Python function | deterministic |
| `tool` | a database, HTTP or MCP operation | deterministic |
| `decide` | ordered rules, then an optional decision model | deterministic → bounded |
| `llm` | one structured model call | bounded |
| `loop` | a model using tools within hard budgets | bounded |
| `human` | a form for a person | external |
| `pending` | a contract waiting for an implementation | — |

Signals route to the next agent or a named end. `tuvl validate` checks contracts, data flow, types
and policy statically and prints each workflow's determinism profile and worst-case token cost. Every
run is **durable and journaled**: it survives restarts, waits for people, can be paused, stepped and
steered, and can be pinned and replayed as a zero-token test fixture.

```yaml
kind: Workflow
version: tuvl/v2
metadata: { name: refund_flow, description: Handle a refund request }
spec:
  models: [Order, Refund]
  trigger:
    http: { path: /api/refunds, method: POST }
    input: { order_id: uuid, reason: str }
  agents:
    - id: load_order
      description: Load the order
      engine: tool
      inputs: { order_id: uuid }
      outputs: { order: Order }
      tool: { use: db, model: Order, op: read, where: { id: "{{ order_id }}" } }
      routes: { default: triage, not_found: END.missing, error: END.failed }
    - id: triage
      description: Decide how the refund is handled
      engine: decide
      inputs: { order: Order, reason: str }
      outputs: { lane: "enum[auto, review]" }
      decide:
        rules:
          - { when: "order.total > 500", then: review }
          - { when: "true", then: auto }
      routes: { auto: issue_refund, review: approval, error: END.failed }
    # … issue_refund (code), approval (human)
  outputs:
    default: { status: 200, body: { refund_id: "{{ refund.id }}" } }
    missing: { status: 404, body: { error: unknown order } }
    failed:  { status: 500, rollback: true, body: { error: refund failed } }
```

---

## Install

**During the 2.0 public beta** (until 20 October 2026):

```bash
uv tool install --prerelease allow "tuvl[standard]==2.0.0b1"   # CLI + Insight (dev UI)
uv tool install "tuvl==2.0.0b1"                                  # CLI only
npm install @tuvl/client@beta                                    # TypeScript client 2.0.0-beta.1
```

uv needs `--prerelease allow` because `[standard]` pulls in `tuvl-insight 2.0.0b1` indirectly.
Report beta issues on [GitHub](https://github.com/tuvl-io/tuvl/issues).

**From 2.0.0:**

```bash
uv tool install "tuvl[standard]"     # CLI + Insight (dev UI)
# or, in a project
uv add "tuvl[standard]"
```

`tuvl` alone is the production server; `[standard]` adds **Insight**, served only in dev mode.

## Quick start

```bash
tuvl init demo --sample --preset gemini -y     # a ticket-triage project with a spec, tests and a lock
cd demo
tuvl validate --strict                         # contracts, data flow, cost
tuvl test                                      # offline tests
tuvl dev --auto-login                          # Insight at http://localhost:8885/insight
```

## Spec-driven

```bash
tuvl spec new refund-automation      # write the intent (Markdown, with checked Data and Flows)
tuvl spec check                      # is the spec complete and consistent? (zero tokens)
tuvl spec verify                     # does the implementation match it, step by step?
tuvl spec status                     # task statuses, derived — never typed in
tuvl codegen                         # schemas, code stubs, tests (no model)
```

The coding agent builds; tuvl referees. Every step above is deterministic and spends no tokens.
Coding agents get the same operations over MCP (`tuvl mcp`) and a skill set in `.agents/skills/`.

The CI gate:

```bash
tuvl validate --strict && tuvl codegen --check && tuvl lock --check && tuvl test && tuvl spec status --strict
```

## Surfaces

- **REST + SSE:** each workflow's route, `POST /api/workflows/{name}/runs`, runs, journal events,
  approvals, operator control; OpenAPI generated from the contracts.
- **MCP export:** `trigger.mcp.enabled` lists a workflow as a tool on `/mcp`.
- **TypeScript:** [`@tuvl/client`](https://www.npmjs.com/package/@tuvl/client) with
  `npx tuvl-client generate` for typed inputs and outputs.
- **Insight:** specs and task board, workflow canvas, runs with breakpoints, step mode and approvals,
  change-engine checks against pinned fixtures, judge calibration.

## Production

```bash
tuvl lock && tuvl ship --tag registry/app:1.0    # gates, Dockerfile (pinned tuvl), Helm chart
tuvl run --role api        # or: worker — scale HTTP and execution separately
```

Ed25519-signed JWT auth, default-deny triggers, scoped CRUD, approvals with no self-approval, secure
fields kept away from models, optional Postgres row-level multi-tenancy.

## Documentation

[tuvl.dev](https://tuvl.dev) · in this repository: [agentic manual](docs/tuvl-agentic-manual.md),
[runtime](docs/runtime.md), [engines](docs/engines.md), [decide](docs/decide.md),
[loop and tools](docs/loop-and-tools.md), [judges](docs/judges.md),
[spec-driven](docs/spec-driven.md), [Insight](docs/insight.md), [API](docs/api.md),
[CLI](docs/cli.md), [security](docs/auth.md), [observability](docs/observability.md).

---

## License & contributing

tuvl is licensed under the **MIT License** — see [LICENSE](LICENSE) — and will
remain MIT-licensed. It is, and will stay, free to use, modify, and distribute.

Development currently happens in a private repository, so the full source is not
yet publicly browsable. This is temporary: the complete codebase will be
published openly in a future release. In the meantime the project is **open to
contribution** — bug reports, feature proposals, and patches are all welcome.
See [CONTRIBUTING](CONTRIBUTING.md) to get started, or reach out at
[developer@tuvl.io](mailto:developer@tuvl.io).
