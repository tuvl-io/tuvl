---
name: ui-run
description: "Start a workflow from a UI and follow it with @tuvl/client: execute, RunHandle, progress events, live model drafts, wait, operator control, and the two error classes."
---
# Starting and following a run

```ts
import { RunHandle, TuvlApiError, TuvlRunError } from "@tuvl/client";

try {
  const result = await client.execute("triage_ticket", { subject, body }, { signal });   // waits up to the trigger's sync_timeout
  if (result instanceof RunHandle) follow(result);      // still running, or waiting on a person
  else showResult(result);                              // the end's body, typed from the generated Workflows
} catch (error) {
  if (error instanceof TuvlRunError) showFailure(error.status, error.body);   // the run ended in an error end, failed or aborted
  else if (error instanceof TuvlApiError) showRefusal(error.status, error.detail);   // 401 403 404 409 422
  else throw error;                                     // incl. a network or CORS failure: show "cannot reach the server"
}
```
| Answer | `execute()` |
|---|---|
| 2xx end | resolves the end's body |
| `202` | resolves a `RunHandle` |
| end with status ≥ 400, failure, abort | rejects `TuvlRunError` (`status`, `body`, `runId`) |
| refused | rejects `TuvlApiError` (`status`, `detail`, `path`); `422` lists the field errors |
| no answer at all | the browser's own `TypeError` ("Failed to fetch"), not a `TuvlApiError`: the backend is down, or in a browser the origin is not allowed (CORS, see `tuvl-ui`) |

- `client.start(name, input)` returns a `RunHandle` at once without waiting. Use it when the screen should show progress from the first moment.
- Never poll the HTTP route again. Follow the run.

## Following a run
```ts
for await (const event of handle.events()) show(event);   // replay from the start, then live, until the run ends
const output = await handle.wait({ timeoutMs: 60_000 }); // resolves the output, rejects TuvlRunError
```
- `RunEvent`: `{ run_id, seq, ts, type, agent_id, engine, tokens, latency_ms, payload }`. Useful types: `agent_started`, `agent_completed`,
  `tool_call`, `decision`, `approval_requested`, `run_completed`, `run_failed`, `run_aborted`. Treat an unknown `type` as ignorable.
- **Survive a refresh:** keep `run_id` (URL or session storage), then `client.run(runId)` gives a handle again;
  `events({ fromSeq })` resumes after the last `seq` you showed. `handle.get()` returns the run: `status`, `current_agent`, `end`, `output`, `error`.
- Statuses: `queued running waiting_human waiting_approval paused completed failed aborted compensated`.
  `waiting_*` and `paused` need a person: `ui-approve`. `wait({ untilWaiting: true })` returns early when the run waits (the output is then `undefined`).
- Stop listening when the screen closes: pass `{ signal }` from an `AbortController`; a cancelled stream releases the connection.

## Live answers while a model writes
Only an `llm` agent declared with `stream_tokens: true` sends drafts (skill `agent-llm`); without it there is nothing to show.
```ts
const live = handle.onToken((piece) => (box.textContent += piece), { onReset: () => (box.textContent = "") });
const output = await handle.wait();   // the final, validated result is always here
await live.done;
```
- A **reset** means the agent retried or its answer was rejected: clear what you showed. Drafts are not saved; the run output is the truth.
- `handle.tokens({ agentId? })` yields only drafts; `handle.stream()` yields journal events and drafts together (`isTokenEvent(e)` tells them apart).

## Operator control
`handle.pause()`, `resume()`, `abort()`, `steer(message)` need scope `runs:control`. Offer them only to a token that has it.
`client.runs.list({ workflow?, status?, limit? })` backs a run list; `client.runs.export(id)` gives the journal as a replayable fixture.
