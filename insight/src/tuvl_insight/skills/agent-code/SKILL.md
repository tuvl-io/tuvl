---
name: agent-code
description: "Implement a code agent: a Python function inside the stub tuvl generate code generates."
---
# Code agents

```yaml
- id: score
  engine: code
  effect: read                 # read | write | destructive; default write (ctx.db refuses writes under read)
  code: { run: score_order, with: { threshold: 500 } }   # with → ctx.params (templates allowed)
  inputs: { order: Order }
  outputs: { score: int }
  routes: { high: review, low: END, error: END.failed }
```

`tuvl generate code` writes/merges `agents/<run>.py`:
```python
from tuvl import Ctx, agent
from agents._generated.schemas import ScoreIn, ScoreOut, ScoreSignal

@agent("score_order")
async def score_order(inp: ScoreIn, ctx: Ctx) -> tuple[ScoreOut, ScoreSignal]:
    """(from the agent's description)"""
    value = int(inp.order.total)            # ← you write only the body
    return ScoreOut(score=value), ("high" if value > ctx.params["threshold"] else "low")
```
- Return the outputs, an `(outputs, signal)` pair, or a bare signal; every signal must be a route.
- Never edit the decorator, signature, annotations, docstring or anything in `agents/_generated/`
  — `tuvl generate code` owns them and rewrites them; your body and parameter names are preserved.
- `ctx.db.get/list/create/update/delete("Model", …)` — only models in the workflow's `spec.models`;
  writes commit atomically with the step. `ctx.http` for outbound calls with
  `ctx.idempotency_key`. `ctx.storage("invoices")` for a named blob store (`put`/`get`/`head`/`list`/`copy`/`delete`/`presign`; read-only inside `effect: read`). `ctx.artifact("artifact://name@1")` for prose. `ctx.log` for logs.
- A `file` input (an upload, skill `tuvl-workflow`) arrives as a reference `{ store, key, name, size, content_type, sha256 }`; read its
  bytes with `await ctx.read_file(inp.invoice)` (optionally `max_bytes`). Never expect the bytes in the input.
- Built-ins need no Python:
  - `code: { run: tuvl.data_search, with: { collection: kb, query: "{{ q }}", top_k: 5, filter: {...}, scope: global } }` with `effect: read` and
    exactly one list output (`passages: "list[json]"`, each `{ content, metadata, score }`).
  - `code: { run: tuvl.data_ingest, with: { collection: kb, document: "{{ text }}", metadata: {...}, scope: global } }` with at most one output
    (the new document's `uuid`). Both need pgvector and a configured collection.
