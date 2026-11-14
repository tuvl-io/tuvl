# Engines

An agent's **engine** implements its contract. The closed set is `pending`, `code`, `tool`,
`decide`, `llm`, `loop`, `human`, `map` and `parallel`. This page covers `pending`, `code`, `tool`, `llm`,
`human`, `map` and `parallel`;
see [decide](decide.md) and [loop and tools](loop-and-tools.md) for the other two.

Every engine except `pending` can emit `error`. Engine parameters live under a key named after the
engine (`code:`, `tool:`, `llm:`, …).

---

## `pending` — a contract without an implementation

```yaml
- id: classify_ticket
  description: Decide which team owns the ticket
  engine: pending
  inputs:  { ticket: Ticket }
  outputs: { team: "enum[billing, technical, general]" }
  routes:  { billing: billing_queue, technical: kb_search, general: general_queue }
```

- `tuvl lint project` checks it fully as a contract (data flow, types, routes). It is a V010 warning.
- In dev and in tests it returns a **typed stub**: schema-valid placeholder outputs and its first
  normal signal. Tests override it with `mocks`.
- `tuvl ship` refuses a project with `pending` agents, and the production runner won't load one.

A `pending` agent is a typed placeholder for a step the spec names but nobody has built yet; `tuvl verify spec` reports it as unfinished and tests through it are *stubbed*, not passed.

---

## `code` — a Python function

```yaml
- id: issue_refund
  description: Calculate the refund amount and record it
  engine: code
  effect: write                           # read | write | destructive; default write
  inputs:  { order: Order, reason: str }
  outputs: { refund: Refund }
  code: { run: issue_refund, with: { currency: EUR } }
  routes: { default: reply, error: END.failed }
```

```python
# agents/issue_refund.py — one @agent per file
from tuvl import agent, Ctx
from agents._generated.schemas import IssueRefundIn, IssueRefundOut


@agent("issue_refund")
async def issue_refund(inp: IssueRefundIn, ctx: Ctx) -> IssueRefundOut:
    """Calculate the refund amount and record it."""
    amount = inp.order.total if inp.reason != "partial" else inp.order.total / 2
    refund = await ctx.db.create("Refund", order_id=inp.order.id, amount=amount)
    return IssueRefundOut(refund=refund)
```

| | |
|---|---|
| Signature | `async def f(inp: <Id>In, ctx: Ctx) -> <Id>Out` or `tuple[<Id>Out, str]`. `tuvl generate code` writes the decorator, signature and docstring; you write the body. |
| Signals | Returning `Out` emits `default`; `(Out, "signal")` emits that signal (it must be routed); an exception emits `error` (`code_exception`). |
| `ctx.db` | Unit of work restricted to `spec.models`; read-only when `effect: read` (a write raises). Writes commit with the step's checkpoint. |
| `ctx` | `ctx.log` (structlog, bound to the run and agent), `ctx.run_id`, `ctx.principal`, `ctx.idempotency_key`, `ctx.http` (shared HTTP client), `ctx.artifact(ref)`, `ctx.params` (`code.with`: `${VAR}` / `${VAR:default}` read the environment, then `{{ }}` templates are rendered), `ctx.agent_id` |
| `effect` | Declared, because it can't be derived from Python. It drives policy (`require_human_before`) and MCP annotations. |

**Configuration and secrets.** A code agent never reads `os.environ` (rule C004). Pass what it needs through `code.with`: `with: { api_key: "${CRM_KEY}", base: "${CRM_URL:https://crm.example.com}" }` reaches the function as `ctx.params`. The references are expanded in the document's own text, before the templates are rendered, so a value that arrives through `{{ }}` is data and is never read as a variable. A variable with no default that is not set fails the agent.

**Built-ins.** `run: tuvl.data_search` and `run: tuvl.data_ingest` are vector search and ingest with
typed contracts, configured by `code.with` (`collection`, `query`/`document`, `top_k`, `filter`,
`metadata`, `scope: global | run`). Search writes `[{content, metadata, score}]` to its single list
output; ingest writes the new document id to its optional single output. The `tuvl.` prefix is
reserved.

**Memory built-ins.** `tuvl.memory_remember` and `tuvl.memory_recall` let an agent remember across runs, in an
ordinary `kind: Collection`:

```yaml
- { id: recall,   engine: code, effect: read, inputs: { question: str }, outputs: { memories: "list[json]" },
    code: { run: tuvl.memory_recall,   with: { collection: support_memory, query: "{{ question }}", window: 5, top_k: 3 } } }
- { id: remember, engine: code, inputs: { turn: str }, outputs: { stored: bool },
    code: { run: tuvl.memory_remember, with: { collection: support_memory, text: "{{ turn }}" } } }
```

- **Whose memory.** The owner is `code.with.key` when given, otherwise the signed-in principal; with neither, the step fails
  (`tool_args_invalid`). Reads and writes are filtered by that owner in the query itself, so a run for one user never sees
  another's memories. Name a `key` from run input only when the trigger is not public: whoever can start the run chooses the owner.
- **Recall** returns the owner's last `window` memories (default 5, 0-50) and up to `top_k` older ones most related to `query`
  (default 3, 0-20), as `[{content, source: recent | related, at | score}]`, older context first. The step journals
  `memory_recalled` with the text it handed to the agent.
- **Remember** stores `text` (embedded) with the owner, run and agent in its metadata. A step that runs again after a crash
  stores it once. What to store is the author's choice: store the agent's answer or a summary, never a `secure` field.
- Treat recalled text as data, not instructions (an earlier user wrote it): put it in the prompt inside a labelled block, as
  tool results are.
- Erase with `tuvl prune memory <collection> --key <owner>`; expire with `--older-than 30d`.
- In tests, mock them by agent id like any code agent.

---

## `tool` — database, HTTP, MCP and storage operations

One engine, four `use` variants. The **effect is derived**; a declared `effect` may only strengthen
it (V015), except that an HTTP `POST`/`PUT`/`PATCH` may declare `effect: read` (search APIs often
read via POST).

### `use: db`

```yaml
tool: { use: db, model: Order, op: read, where: { id: "{{ order_id }}" } }
tool: { use: db, model: Refund, op: create, values: { order_id: "{{ order.id }}", amount: "{{ amount }}" } }
tool: { use: db, model: Ticket, op: list, where: { status: open }, order_by: [-created_at], limit: 20 }
```

`op`: `create`, `read`, `update`, `delete`, `list`, `upsert`. The model must be in `spec.models`
(V018). The result lands in the agent's single output — a row, or `list[Model]` for `list`; `delete`
has none. Untyped `where` values are coerced to the column type.

| op | Effect | Reserved signals |
|---|---|---|
| `read` | read | `not_found` |
| `list` | read | — |
| `create`, `update`, `upsert` | write | `conflict` |
| `delete` | destructive | `not_found` |

### `use: http`

```yaml
tool:
  use: http
  method: POST
  url: https://payments.example.com/refunds
  headers: { Authorization: "Bearer ${PAYMENTS_KEY}" }
  body: { order: "{{ order.id }}", amount: "{{ amount }}" }
  expect: { status: [200, 201] }
  map: { refund_ref: response.body.id }   # response.status | headers | body paths → outputs
  idempotency: auto                       # auto | none — sends Idempotency-Key
```

`GET`/`HEAD` are `read`; other methods are `write` (override to `destructive` allowed). Without
`map`, a single output receives the body. An unexpected status emits `http_<status>` when that
signal is routed; otherwise `error` (`rate_limit` for 429 and `transient` for 5xx are retryable,
`http_error` otherwise). `${VAR}` reads the environment.

### `use: storage`

```yaml
tool: { use: storage, store: invoices, op: put, key: "{{ id }}.pdf", body: "{{ pdf_b64 }}", encoding: base64 }
tool: { use: storage, store: invoices, op: get, key: "{{ id }}.pdf", as: base64 }
```

`op`: `put`, `get`, `head`, `list`, `copy`, `delete`, `presign`, on a store declared in
`storage/*.yaml` (S3 and compatible services, Azure Blob, GCS, local disk). The result lands in the
single output — the blob for `put`/`head`/`copy`, the content for `get`, the items for `list`, the
URL for `presign` — or is spread by `map`. `get`, `head` and `copy` emit `not_found`; `put` and
`copy` emit `conflict` unless `if_exists: overwrite`; `delete` is destructive. Object bytes are never
journaled. Details: `docs/storage.md`.

### `use: mcp`

```yaml
tool: { use: mcp, server: artifact://zendesk@3, name: add_comment, args: { ticket: "{{ ticket.id }}", body: "{{ text }}" } }
```

The effect comes from the server's `readOnlyHint`/`destructiveHint`, else `write`. The tool's
structured content (or parsed text) fills a single output, or spreads an object across several.
The tool's input schema is pinned in `tuvl.lock`.

Each worker process keeps the MCP session open between calls, one per tenant and server (a stdio
server is started once, not per call). A change to the server's artifact or to an environment
variable it reads opens a new session; a session that dies is replaced on the next call. An unused
session closes after `TUVL_MCP_IDLE_SECONDS` (default 300); `0` opens a new session for every call.

### Side effects and crashes

Runs are at-least-once per agent ([runtime](runtime.md)). `tool: db` writes on the primary
datasource commit with the step's checkpoint (exactly-once under `transaction: per_agent`); HTTP
calls get a stable `Idempotency-Key`; MCP calls are at-least-once, so give write/destructive tools a
`compensate` agent (V015).

---

## `llm` — one structured model call

```yaml
- id: reply
  description: Write the customer confirmation
  engine: llm
  inputs:  { order: Order, refund: Refund }
  outputs: { text: str }
  budget:  { timeout: 20s, max_tokens: 1500, retry: { attempts: 2, errors: [parse_error, timeout] } }
  llm:
    model: default                        # AgentModel name (type llm) or a provider/model id (see `docs/models.md`)
    system: artifact://support-voice@3    # text or a prompt artifact
    prompt: Write a short confirmation of the refund.
    skills: [artifact://refund-policy@2]
  guardrails: { output: [artifact://refund-reply-quality@2] }
  routes: { default: END, guardrail_violation: END.failed, error: END.failed,
            parse_error: END.failed, timeout: END.failed, budget_exceeded: END.failed }
```

- Every input is serialised into the prompt inside a `<data name="…" type="…">` fence, and the
  system prompt tells the model that fenced text is data, never instructions. A value can't close
  its fence early (`</data>` inside it is escaped), so retrieved documents, user text or web
  content can't pass themselves off as part of the prompt. `loop` agents fence every tool result the
  same way (`<data tool="…">`). Text you interpolate into `llm.prompt` with `{{ … }}` is not fenced:
  pass untrusted values as inputs.
- The model answers with **structured output** whose schema is the agent's `outputs`.
- With more than one normal route, an `outcome: enum[<those signals>]` field is added and its value
  becomes the signal; a free-form model string can never become a signal.
- `budget.max_tokens` and `budget.timeout` are required (V011).
- Guardrails: `input`/`output` lists of `guardrail` or `judge` artifacts. A failing output judge emits
  `guardrail_violation`; an unavailable or uncertain judge fails closed (`error`).
- **Showing the answer while it is written.** `llm: { stream_tokens: true, stream_field: draft }` sends the text of one `str` output
  to the run's event stream as the model produces it (`token_delta` events, see [API](api.md#sse-events)), so a UI is not frozen
  for the length of the call. `stream_field` names the output; it can be left out when the agent has exactly one `str` output. The model
  still returns its normal structured answer: the result, its validation, tokens, retries and journal are unchanged, and the draft is
  only a preview. When the answer is then rejected (`parse_error`, a cut-off `budget_exceeded`, an error in the stream) or the agent runs
  again, the client receives `token_reset` and discards the draft. Not allowed together with `guardrails.output` (V039): the text would
  reach a user before a guardrail could refuse it. A model that reasons before it answers sends nothing until its answer starts.
- Secure model fields can't be inputs unless `policy.allow_secure_to_llm: true` (V020).

---

### Remembering across runs (`llm.memory`, `loop.memory`)

```yaml
llm:
  model: gpt-4o-mini
  prompt: "Answer the customer: {{ question }}"
  memory:
    collection: support_memory     # a kind: Collection that holds the memories
    key: principal                 # whose memory: principal (default) | tenant | "{{ some_input }}"
    window: 5                      # the owner's latest memories handed to the agent
    recall_top_k: 3                # plus older ones related to the prompt
    max_tokens: 1000               # most recalled text in the prompt (about 4 characters a token)
    write: [question, answer]      # inputs and outputs stored after a successful run; default: every output; [] only reads
    retention: 90d                 # optional: memories older than this are deleted by the runtime
```

Before the call the owner's memories are added to the prompt under "Earlier context (from previous sessions)" inside a
`<data name="memory">` fence, and the model is told that text in `<data>` is data, never instructions. After a successful
answer the `write` fields are stored (in the step's own transaction, once even if the step runs again). Journal events:
`memory_recalled` (what was handed over) and `memory_written`.

- The owner is chosen in YAML, never by the model. A key templated from input on a public trigger is refused (V043):
  anyone could name another person's memory. A run with no owner fails with `tool_args_invalid`.
- `write` may name only fields of the agent, and never one that carries secure model fields (V043).
- `max_tokens` is added to the agent's worst-case tokens, so `policy.max_tokens_per_run` accounts for memory.
- `retention` deletes memories past that age, for every agent that names the collection (the shortest wins); the runtime
  sweeps every five minutes. Erase by hand with `tuvl prune memory` or the Memory page in Insight.
- **Compaction.** `tuvl.memory_compact` (`collection`, `model`, `keep`: default 5, `min_rows`: default 20) is a built-in to
  run from a scheduled workflow: for each owner with more than `min_rows` memories it asks the model to fold all but the
  latest `keep` into one summary row (same owner, the time of the newest row it replaces) and deletes the originals in the
  same transaction. It writes the number of owners compacted to its one output and journals `memory_compacted`.
- A `loop` agent has the same block (`loop.memory`): the recalled text goes under the opening input, the same
  rules and journal events apply, and the `write` fields are stored when the loop finishes with a result. A loop
  that fails, waits for an approval or runs out of budget stores nothing.

## `human` — a person fills a form

```yaml
- id: approval
  description: A manager approves or rejects a refund
  engine: human
  inputs:  { order: Order, message: str }
  outputs: { approved: bool, note: "str?" }   # the form
  human:
    title: "Refund for order {{ order.id }}"
    instruction: Approve this refund?
    show: [order, message]                  # inputs shown to the reviewer
    form:                                   # optional; defaults from outputs
      approved: { label: "Approve?", widget: toggle }
      note:     { label: Note, widget: textarea }
    required_group: support_manager
    assignee: "{{ order.owner_id }}"
    expires: 48h
    route_on: approved                      # bool → "true"/"false"; enum → its values
  routes: { "true": issue_refund, "false": END.rejected, expired: END.failed, error: END.failed }
```

- The run checkpoints, opens an **approval request** and becomes `waiting_human`. A synchronous
  trigger answers `202` with the run descriptor and the request.
- Answer with `POST /api/approvals/{id}` `{ values }` (or `POST /api/runs/{run_id}/human/{agent_id}`).
  Values are validated against `outputs`; the run is re-queued and continues on any worker.
- Who may answer: with `required_group`, a holder of `approvals:decide` in that group who is not the
  triggering principal; otherwise the triggering principal or `iam:admin`. `iam:admin` bypasses the
  group, never the no-self-approval rule.
- `expires` closes the request and takes the `expired` route; a late answer gets `409`.
- `form` fields must equal `outputs`; `route_on` must be a bool or enum output (V016).

In tests, an unmocked human agent stops the run: assert it with `expect.wait: human`.

---

## `map` — a workflow for every item of a list

```yaml
- id: score_each
  description: Score every candidate
  engine: map
  inputs:  { candidates: "list[json]", job_id: uuid }
  outputs: { scores: "list[json]" }          # one list: the results, in input order
  map:
    over: candidates                         # an input that is a list
    workflow: score_candidate                # the child workflow, run once per item
    as: candidate                            # the child's trigger input that receives the item (default: item)
    with: { job_id: "{{ job_id }}" }         # further child inputs, templates over this agent's inputs
    concurrency: 4                           # items in flight (1-32, default 4)
    max_items: 100                           # longer lists fail the agent before any item starts (default 100)
    on_error: collect                        # fail (default) | collect
  effect: write                              # the strongest effect of the child; never weaker (V015)
  routes: { default: rank, partial: review, error: END.failed }
```

Fan-out and nothing else: tuvl runs the child workflow once per item, a few at a time, and gathers
the results. The step is one agent of the parent; each item is a **child run** of its own.

- **Each item is a run.** It has its own journal, checkpoints and cost, shows in the run list with
  `parent_run_id` set, and is replayable on its own. The step journals `map_started`, one `map_item`
  per item, and `map_finished`.
- **Idempotent after a crash.** The child of item *i* is keyed `<agent>:<i>`. A parent that is
  reclaimed by another worker and reaches the step again follows the children already started (or
  finished) instead of starting new ones, so no item runs twice.
- **Success is per item.** An item succeeds when its child completes on an end whose status is below
  400; its result is that end's `body`. The result list keeps the input order.
- **`on_error: fail`** stops starting new items at the first failed item and emits `error`
  (`map_item_failed`: which item, why, and how many failed or never started). Items already running
  finish; their runs stay in the journal.
- **`on_error: collect`** runs every item. A failed item is `null` in the results, and the agent
  emits `partial` instead of `default` when the workflow routes `partial` (otherwise `default`).
  `partial` is only valid with `collect` (V004).
- **A mapped workflow must finish on its own (V036):** no `human` or `pending` agent and no loop tool
  that needs approval. A child that waits anyway fails its item.
- **Bounded.** `concurrency` limits parallel items, `max_items` the list length, and
  `policy.max_depth` how deeply workflows nest (a workflow cannot map itself, V035). Worst-case
  tokens in `tuvl lint project` are the child's worst case times `max_items`, and the step takes the
  child's strongest determinism class.
- **Checks (V035):** `over` is a list input; the workflow exists; `as` and every `with` key are inputs of
  the child; no required child input is left out; item and input types agree (V007); `outputs` is
  exactly one list (V007).
- Items run in this worker's process, in parallel; another worker takes over only through crash
  recovery. Nothing is shared between items: they are independent runs, so a map is not a fork/join of
  different branches, only the same workflow over many items.

---

## `parallel` — different read-only lookups at the same time

```yaml
- id: gather
  description: Look the customer up in three places at once
  engine: parallel
  inputs:  { customer_id: uuid, name: str }
  outputs: { profile: json, hit: bool, notes: "list[json]" }   # exactly the branches' outputs together
  parallel:
    on_error: fail                           # fail (default) | collect
    concurrency: 3                           # branches in flight (default: all)
    branches:                                # 2-16 agents; each reads its `inputs` from this step's inputs
      - id: crm
        engine: tool
        inputs:  { customer_id: uuid }
        outputs: { profile: json }
        tool: { use: db, model: Customer, op: read, where: { id: "{{ customer_id }}" } }
      - id: sanctions
        engine: tool
        inputs:  { name: str }
        outputs: { hit: bool }
        tool: { use: http, method: GET, url: "https://screening.example/check?q={{ name }}", map: { hit: response.body.hit } }
      - id: knowledge
        engine: llm
        inputs:  { name: str }
        outputs: { notes: "list[json]" }
        llm: { model: gpt-4o-mini, prompt: "List what we know about {{ name }}" }
        budget: { max_tokens: 800, timeout: 20s }
  budget: { timeout: 30s }
  routes: { default: decide, error: END.failed }
```

The step takes about as long as its slowest branch instead of the sum. It is one agent of the workflow: one checkpoint,
one route, one place in the journal.

- **Branches only read (V040).** A branch is a `tool` (db `read`/`list`, http `GET`/`HEAD`, storage reads), an `llm`, or a
  `code` agent with `effect: read`. Writes, destructive tools and MCP tools (their effect is unknown until listed) belong
  in a step of their own; so do `loop`, `human`, `decide`, `map` and a nested `parallel`.
- **A session of its own.** Each branch runs in its own database session with the idempotency key
  `<step key>:<branch id>`; `transaction: per_run` is refused (V042).
- **Outputs.** The step's outputs are all branches' outputs together; no name twice, nothing left over (V041). A branch
  ends on `default`; anything else is a failure.
- **`on_error: fail`** stops the branches still running at the first failure and emits `error`
  (`parallel_branch_failed`, naming the branch). **`collect`** lets every branch finish; a failed branch's outputs are
  `null` (declare them optional), and the step emits `partial` when the workflow routes it, `default` otherwise.
- **Timeout.** `budget.timeout` is for the whole step; when it passes, every branch is cancelled and the step emits `timeout`.
- **Journal.** `parallel_started`, one `parallel_branch` per branch in the order they finish (ok, ms, tokens, reason),
  `parallel_finished`. A branch's own events (`tool_call`, `llm_turn`, ...) carry `branch` in the payload.
- **Cost.** The worst-case tokens of a step are the sum of its branches' `max_tokens`; its determinism class is its
  strongest branch's.
- **Crash.** A step is checkpointed when it completes, so a crash in the middle runs the whole step again; the branch
  keys make repeating a read or a model call safe.
- **Offline tests.** `tuvl run tests` runs the step when every branch is mocked (`mocks.crm`, `mocks.sanctions`, ...) and
  names the first branch that is not; mock the step itself (`mocks.gather`) to skip its branches.
- **Codegen.** `tuvl generate code` writes stubs for `code` branches like any other code agent.

