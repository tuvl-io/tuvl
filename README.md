<p align="center">
  <img src="https://raw.githubusercontent.com/tuvl-io/tuvl/main/assets/logo.png" alt="" width="40" />&nbsp;&nbsp;<span style="font-size:2em"><strong>tuvl</strong></span><br/>
  <sub>/ˈtuːvəl/ &nbsp;·&nbsp; തൂവൽ &nbsp;·&nbsp; feather</sub>
</p>

<p align="center">
  <strong>Intelligent workflows. Deterministic execution.</strong>
</p>

<p align="center">
  Declare the workflow in YAML. tuvl checks it before it runs, bounds every model call,<br/>
  journals every step and refuses anything the contract doesn't declare.<br/>
  Python only where you need it. Served as REST, SSE and MCP.
</p>

<p align="center">
  <a href="https://tuvl.io">Website</a> &nbsp;·&nbsp;
  <a href="https://tuvl.dev">Docs</a> &nbsp;·&nbsp;
  <a href="https://github.com/tuvl-io/tuvl">GitHub</a>
</p>

> Versions follow [SemVer](https://semver.org); every change is in the [CHANGELOG](CHANGELOG.md).

A tuvl workflow is a graph of **agents**. Each agent has a typed contract — `inputs`, `outputs`, the
signals it emits — and an **engine** that implements it:

| Engine | | Determinism |
|---|---|---|
| `code` | a Python function | deterministic |
| `tool` | a database, HTTP, blob storage or MCP operation | deterministic |
| `decide` | ordered rules, then an optional decision model | deterministic → bounded |
| `llm` | one structured model call | bounded |
| `loop` | a model using tools within hard budgets | bounded |
| `map` | a workflow run once for every item of a list | as the child |
| `parallel` | different read-only lookups at the same time | as the branches |
| `human` | a form for a person | external |
| `pending` | a contract waiting for an implementation | — |

Signals route to the next agent or a named end. `tuvl lint project` checks contracts, data flow, types
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

```bash
uv tool install "tuvl[standard]"     # CLI + Insight (dev UI)
# or, in a project
uv add "tuvl[standard]"
```

`tuvl` alone is the engine and the production CLI: it parses the YAML, talks to the database and serves the API
(`tuvl serve app`, `run workflow`, `list runs`, `check system`, `generate key`, ...). `[standard]` adds the
`tuvl-insight` package: **Insight** (served only in dev mode) and the development tools (`tuvl init`, `dev`, `ship`,
`lint project`, `generate code`, `run tests`, `serve mcp`). On a bare install those commands answer with how to install
them. See [Core and Insight](docs/core-and-insight.md). Features with their own packages are
extras: `tuvl[mcp]`, `tuvl[redis]`, `tuvl[vector]`, `tuvl[otel-grpc]`. `tuvl ship` installs only the ones a project uses.

## Quick start

```bash
tuvl init demo --sample --preset gemini -y     # a ticket-triage project with a spec, tests and a lock
cd demo
tuvl lint project --strict                         # contracts, data flow, cost
tuvl run tests                                      # offline tests
tuvl dev --auto-login                          # Insight at http://localhost:8885/insight
```

## Spec-driven

```bash
tuvl create spec refund-automation      # write the intent (Markdown, with checked Data and Flows)
tuvl lint spec                      # is the spec complete and consistent? (zero tokens)
tuvl verify spec                     # does the implementation match it, step by step?
tuvl list tasks                     # task statuses, derived — never typed in
tuvl generate code                         # schemas, code stubs, tests (no model)
```

The coding agent builds; tuvl referees. Every step above is deterministic and spends no tokens.
Coding agents get the same operations over MCP (`tuvl serve mcp`) and a skill set in `.agents/skills/`.

The CI gate:

```bash
tuvl lint project --strict && tuvl generate code --check && tuvl verify lock && tuvl run tests && tuvl list tasks --strict
```


## Authoring with your coding agent

Already have Claude Code, Codex or Gemini CLI? tuvl can drive it from an idea to a packaged project, and
accept only what its own checks (no model in the loop) pass:

```bash
tuvl import idea brief.pdf           # IDEA-SK-0004: a numbered, immutable copy of the brief
                                     # (or --url <web page, Jira or GitHub issue>: a snapshot of it)
tuvl generate spec --idea IDEA-SK-0004    # your agent drafts SPEC-SK-0004-01 on a branch; a person approves it
tuvl approve spec SPEC-SK-0004-01 && tuvl generate tasks SPEC-SK-0004-01
tuvl generate code --spec SPEC-SK-0004-01 --all   # models and skeletons first, then your agent, one PR per task
tuvl review code && tuvl ship --format both      # gate, image and a runnable archive
```

`tuvl dev` shows the same path in a terminal UI, and every step is in a local audit ledger. Details:
[docs/authoring.md](docs/authoring.md).

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
tuvl generate lock && tuvl ship --tag registry/app:prod    # gates, Dockerfile (pinned tuvl + only the extras you use), Helm chart
tuvl serve app --role api        # or: worker — scale HTTP and execution separately
```

Ed25519-signed JWT auth, default-deny triggers, scoped CRUD, approvals with no self-approval, secure
fields kept away from models, optional Postgres row-level multi-tenancy.

## Documentation

[tuvl.dev](https://tuvl.dev) · in this repository: [agentic manual](docs/tuvl-agentic-manual.md),
[runtime](docs/runtime.md), [engines](docs/engines.md), [decide](docs/decide.md),
[loop and tools](docs/loop-and-tools.md), [judges](docs/judges.md),
[storage](docs/storage.md), [models](docs/models.md), [spec-driven](docs/spec-driven.md), [Insight](docs/insight.md), [API](docs/api.md),
[CLI](docs/cli.md), [security](docs/auth.md), [observability](docs/observability.md).

---

## Verifying a release

From 2.0.0, `tuvl` and `tuvl-insight` are published to PyPI by a GitHub Actions workflow in this repository, using PyPI Trusted Publishing, and each file carries a signed [provenance attestation](https://docs.pypi.org/attestations/) naming this repository and `publish.yml`.

PyPI may show **"The repository or configuration that created this release differs from a previous release"**. It compares each release's publisher with the previous one, and earlier releases came from elsewhere:

| Release | Published from |
|---|---|
| Releases before 2.0 | A build server, without provenance |
| 2.0.0b1 to 2.0.0b10 | A private repository (`tuvl-io/tuvl-private`), with provenance for some of them |
| 2.0.0 and later | This repository, `tuvl-io/tuvl` |

The publisher changed once, when the source moved here. The notice does not mean a release was altered. To check one yourself, run this in the folder you downloaded to:

```bash
pip download tuvl==2.0.0 --no-deps
pypi-attestations verify pypi --repository https://github.com/tuvl-io/tuvl pypi:tuvl-2.0.0-py3-none-any.whl
```

More in the [installation guide](https://tuvl.dev/getting-started/installation/#verifying-a-release-and-the-pypi-provenance-notice).

## License & contributing

tuvl is licensed under the **Apache License, Version 2.0** — see [LICENSE](LICENSE) and
[NOTICE](NOTICE). It is free to use, modify and distribute, with an express patent grant.
The name "tuvl" and the logos are not licensed for use as a brand (Apache-2.0 §6).

Releases before `2.0.0b5` keep the terms they were published with. From `2.0.0b5` tuvl is
Apache-2.0; see the [changelog](CHANGELOG.md).

The source of every release is published at [tuvl-io/tuvl](https://github.com/tuvl-io/tuvl), tagged with the
release version. Bug reports, feature proposals and patches are welcome; see
[CONTRIBUTING](CONTRIBUTING.md) for how a change lands and the [code of conduct](CODE_OF_CONDUCT.md).
Security reports go to [security@tuvl.io](mailto:security@tuvl.io), never to a public issue; see
[SECURITY](SECURITY.md). Anything else: [developer@tuvl.io](mailto:developer@tuvl.io).
