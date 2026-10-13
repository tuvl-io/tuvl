# Insight

Insight is tuvl's browser UI for building and debugging a project. It is served by `tuvl dev`
(dev mode only) at `/insight`, talks to the server over REST and SSE, and edits the **same YAML
files** as your editor and coding agents — there is no separate canvas model.

```bash
tuvl dev                 # http://localhost:8885/insight — asks for the session key
tuvl dev --auto-login    # skip the key screen on this machine
```

## Pages

| Page | What it does |
|---|---|
| **Specs** | Every spec is a tile across the top (task progress, stale/blocked markers, a ✗ badge for blocking `spec check` findings); click one to show it below, double-click (or **Open spec**) to edit it in its own window, with `spec check` findings inline — saving there refreshes the page. Below: **Tasks** (the coding agent's task list, statuses derived), **Findings** (blocking, warnings, waivers, unchecked prose) and **Traceability** (`spec verify`: step → agent ✓ / pending / mismatch → the tests that run it, each example's test result with why; a step opens its node on the canvas, and the canvas links back) |
| | On the canvas, **Run** / **Step** are disabled while a model the workflow uses isn't configured (`/health` → `tuvl doctor`) |
| **Workflows** | Canvas and YAML editor for each workflow (below) |
| | A `code` agent's **Engine** tab is a Python editor for `agents/<file>.py` (⤢ full screen, ⌘S / **Save Python**). Saving is syntax-checked, normalized by codegen (decorator, signature and docstring come from the contract; your body is kept) and reloads the agent. **✦ Assist** writes or changes the body with one of the project's LLM AgentModels: it is given the agent's contract, the fields of its models and the `agent-code` rules, returns the whole file into the editor (↶ undo) and saves nothing; unconfigured models are disabled |
| **Runs** | Live and past runs, journal timeline per agent (loop turns, tool calls, decisions, judge verdicts), approvals inbox with forms and tool-call approve/edit/reject, pause/resume/abort/steer, start a run, pin a run as a fixture, replay fixtures (SAME/DIFF) |
| **Artifacts** | Prompts, steering, skills, guardrails, hooks, MCP servers; **judge editor** with try/calibrate; **rules editor** with "which rule matches" |
| **Models**, **Datasources**, **Embeddings**, **Collections** | Data configuration |
| **AI Models** | `AgentModel`s — LLM (LiteLLM id, base URL, key reference, parameters) or **Decision** (provider `laya`/`jev`/`litellm` + model) |
| **IAM**, **Federation**, **Settings**, **API Docs** | Users and roles, OAuth providers, system settings (incl. the CRUD kill switch), OpenAPI |

## Workflows: the canvas

- **Start card**: trigger (HTTP path/method/public, schedule, MCP export) and the typed input.
- **Agent cards**: engine, determinism class, `id`, description, input/output contract, budget and
  retry chips, one outlet per routable signal. Red ports mark inputs not produced upstream.
- **End cards**: one per named output, with status and body.
- **Palette**: Deterministic (Code, Database op, HTTP call, MCP tool, Vector search, Vector ingest),
  AI (LLM call, Autonomous loop), Control (Decide, Human approval), Contract only (Pending agent). A new
  agent is wired after the selected one when that one ended the workflow.
- **Panel tabs**: Contract (fields with type suggestions and upstream outputs) · Engine (rules table
  for decide, a YAML block editor per engine) · Budget · Governance · Routes · Probe (run this one
  agent with hand-entered inputs).
- **Summary bar**: determinism profile, worst-case tokens and live validation of the draft; a
  data-flow overlay draws each output key to its consumers.
- Canvas and YAML tabs edit the same text (comments are kept); node positions are stored in
  `.tuvl/layout/<workflow>.json`. **Save** writes the file; **Discard** drops the draft.

### Run, debug, pin

- **▶ Run** starts a dev run with an input form; **⏯ Step** pauses before every agent and every loop
  tool call.
- Click a card's dot to set a **breakpoint** (`.tuvl/breakpoints.json`): dev runs pause before that
  agent. While paused, the Runs page shows the agent's input view; edit it and **Continue**.
- **📌 Pin as fixture** on a finished run writes `tests/fixtures/<workflow>/<name>.journal.json`.

### Change engine

**Change engine…** swaps an agent's engine and keeps its id, inputs, outputs and routes (the
signals of a code agent become the decision enum when switching to decide). Then:

- **⇄ Check vs fixtures** replays the workflow's pinned fixtures against the **unsaved draft**, with
  only that agent executing and everything else served from the recording — SAME/DIFF per fixture,
  plus the validate profile before and after;
- **⌘ Generate stub** runs codegen when the new engine is `code`.

Breakpoints and step mode never apply to these replays or to `tuvl test`.

## Artifacts: judges and rules

- **Judge** — model, rubric, `min_score`, `min_agreement`. **Try**: paste a subject, ask the judge
  (live, using the draft), then mark the verdict ✓ right / ✗ wrong with a label to add it to the
  calibration set (`fixtures/judge/<label>.json`). **Calibration**: run the set and see agreement.
  See [judges](judges.md).
- **Rules** — an ordered `when → then` table (edit, reorder, add, remove) and a context box that
  shows which rule matches.
- **+ New artifact** creates prose artifacts, or judge/rules YAML from a template.

Saving a YAML file reloads the dev server; Insight picks the change up when it is back.

## Dev API

Insight uses `/dev/*` (dev mode only, behind the session key; in dev mode the key is also an
`iam:admin` token for `/api/*`): project summary, raw text files per area, validate (also for a
draft), schema, codegen, tests, specs, layout, breakpoints, runs (start, context, pin, replay),
agent probe, change check, judges and rules. The OpenAPI at `/docs` lists them.
