---
name: agent-llm
description: "Implement an llm agent: one model call with typed outputs, prompts as artifacts, guardrails and budgets."
---
# llm agents

```yaml
- id: classify
  engine: llm
  inputs: { subject: str, body: str }
  outputs: { category: "enum[billing, technical]", summary: str }   # = the structured output
  budget: { timeout: 30s, max_tokens: 500, retry: { attempts: 2, errors: [parse_error, timeout] } }
  llm:
    model: default                     # an AgentModel in llms/ (pinned in tuvl.lock)
    system: artifact://triage-prompt@1 # prompts are versioned artifacts
    prompt: Classify this ticket.
  guardrails: { output: [artifact://no-secrets@1] }
  routes: { default: next, parse_error: END.failed, timeout: END.failed,
            budget_exceeded: END.failed, guardrail_violation: END.failed, error: END.failed }
```
- Required routes: `parse_error`, `timeout`, `budget_exceeded`, `error` (+ `guardrail_violation`
  with guardrails). An enum output can drive routing (`outputs: { route: "enum[a, b]" }` → routes a/b).
- Prompts: `artifacts/<name>.md` with front matter `name`, `type: prompt`, `version`. Bump the
  version on change and reference `@version`.
- Guardrails are artifacts: a `type: judge` artifact (skill `tuvl-test`) or deterministic checks:
  ```yaml
  kind: Artifact
  metadata: { name: no-secrets, version: 1 }
  spec:
    type: guardrail
    checks:
      - { check: regex_deny, patterns: ["(?i)internal[_-]?ref"] }
      - { check: max_chars, limit: 4000 }        # also: json_schema, pii_mask
  ```
- To remember across runs, add `llm.memory: { collection: support_memory, key: principal, window: 5, recall_top_k: 3, max_tokens: 1000, write: [question, answer] }`:
  the owner's earlier turns are put in the prompt as data and the `write` fields are stored afterwards. The owner is `principal`, `tenant` or
  a template over inputs (refused on a public trigger, V043). `tuvl prune memory` erases it.
- To let a UI show the answer while the model writes it, set `llm: { stream_tokens: true, stream_field: draft }`: the text of that one `str`
  output goes to the run's event stream (`?drafts=true`, `@tuvl/client` `onToken`). The result, validation and retries are unchanged. Name
  `stream_field` when there are several `str` outputs; it is refused with `guardrails.output` (V039).
- Never pass `secure: true` fields to a model (V020) — project them out with a code agent.
- Tests mock llm agents: `mocks: { classify: { outputs: {...} } }`; or replay a pinned run.
