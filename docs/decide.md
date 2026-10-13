# The `decide` engine

`decide` routes a run through **ordered rules first**, then — only if no rule matches — an optional
**decision model**. Rules-only agents are deterministic and free; a model makes the agent `bounded`.

```yaml
- id: triage_refund
  description: Decide how a refund is handled
  engine: decide
  inputs:  { order: Order, reason: str, message: str }
  outputs: { route: "enum[auto_refund, manager_review, reject]" }   # exactly one enum output
  budget:  { timeout: 2s, retry: { attempts: 1 } }                # applies to the model call
  decide:
    rules:                                 # first match wins
      - { when: "order.total > 500", then: manager_review }
      - { when: "reason == 'fraud_flag'", then: reject }
    # or: rules: artifact://refund-rules@4   (a `type: rules` artifact)
    model:                                 # optional
      ref: triage-classifier               # AgentModel (type: decision) or an llm AgentModel
      min_confidence: 0.8
    shadow: false                          # true: rules decide; the model runs alongside, journaled only
  routes:
    auto_refund:    issue_refund
    manager_review: approval
    reject:         END.rejected
    low_confidence: approval               # required when a model is set
    error:          END.failed             # no_rule_matched, decision_model_unavailable
```

## Semantics

| Situation | Result |
|---|---|
| A rule matches | Its `then`. **The model is not called.** Source `rule#<n>` |
| No rule matches, no model | `error` (`no_rule_matched`) |
| No rule matches, model confident (≥ `min_confidence`) | The model's value. Source `model` |
| No rule matches, model below `min_confidence` | `low_confidence` (proposal and confidence journaled) |
| No rule matches, model **unavailable** (down, auth, timeout after retries) | `error` (`decision_model_unavailable`). **Never** a fallback to rules, a default or another model |
| `shadow: true` | Rules decide (`no_rule_matched` if none matches). The model runs every time; its answer is journaled for comparison and its failures are logged, not errors |

Rules are TEL conditions over the agent's inputs, type-checked by `tuvl validate`. The output enum
must equal the set of normal route keys, and every `then` must be one of its values (V012). A rule
`{ when: "true", then: x }` at the end is the usual default.

Every invocation journals a `decision` event: `{ decision, source: rule#n | model | shadow,
confidence?, model_ref?, latency_ms }`. Tests assert on it with
`expect.decisions: { triage_refund: { source: "rule#1" } }`; Insight shows it on the run timeline.

## Decision models

```yaml
kind: AgentModel
metadata: { name: triage-classifier }
spec:
  type: decision
  provider: litellm                      # laya | jev | litellm
  model: gemini/gemini-3.1-flash-lite    # pinned in tuvl.lock
```

### Laya and Jev (System One)

`laya` and `jev` speak the System One protocol (`POST /v1/systemone`): the agent's inputs are sent
as the `state`, and its enum output as one `choice` question. The answer comes back with a
probability for every option, and tuvl uses the server's calibrated confidence for `min_confidence`.

```yaml
kind: AgentModel
metadata: { name: laya }
spec:
  type: decision
  provider: laya
  model: auto                            # auto | english | multilingual | typed-decisions
  api_base: ${LAYA_API_BASE:http://localhost:8000}
  api_key: ${LAYA_API_KEY:}              # optional; must be an ${ENV_VAR} reference
```

- **Laya** is self-hosted: the `laya-serve` image serves it on CPU with its weights pinned and
  verified, so the image version is what pins the model. `model: auto` lets Laya route by
  language (English or multilingual); `tuvl init --decision-model laya` scaffolds this model, a
  `compose.yaml` for local development, and `tuvl ship` adds Laya to the Helm chart.
- **Jev** is TypeSafe's hosted service: set `api_base` to its endpoint and pin `model` to a version
  (`jev-1.13.0`); an unpinned Jev model is a V026 warning.
- Confidence thresholds do not transfer between Laya and Jev (or between checkpoints) — calibrate
  `min_confidence` for the model you deploy.

### OpenAI-compatible endpoints

A decision model behind an OpenAI-compatible endpoint (a LiteLLM proxy, a self-hosted classifier)
connects through `provider: litellm`:

```yaml
kind: AgentModel
metadata: { name: hosted-classifier }
spec:
  type: decision
  provider: litellm
  model: openai/my-classifier            # LiteLLM's OpenAI-compatible route
  api_base: ${CLASSIFIER_API_BASE}
  api_key: ${CLASSIFIER_API_KEY}         # must be an ${ENV_VAR} reference
```

- `litellm` asks the model for `{"decision": <one of the choices>, "confidence": <0..1>}` as
  structured output. An `llm`-type AgentModel can also be referenced directly; it uses the same
  adapter.
- **Where the confidence comes from.** A `type: decision` model's reported `confidence` is used
  first, then token logprobs. An `llm` model's logprobs come first, because a general LLM's
  self-reported confidence is poorly calibrated. An answer with neither (or a confidence outside
  0..1) is `decision_model_unavailable` — never treated as certain.

### All providers

- Other providers can be registered through the `tuvl.decision_providers` entry point.
- A decide agent with an LLM-backed model must declare `budget.max_tokens` and `budget.timeout`.
- `/health` reports each configured decision model's readiness.

## Rules as artifacts

A `type: rules` artifact is a reusable decision table, edited and tried in Insight (Artifacts page):

```yaml
kind: Artifact
metadata: { name: refund-rules, version: 4 }
spec:
  type: rules
  rules:
    - { when: "order.total > 500", then: manager_review }
    - { when: "true", then: auto_refund }
```

## Policy

`policy.model_only_decisions: warn | error | allow` flags (V013) a model-only decision (no rules) that
feeds a write or destructive effect without a human, approval or `compensate`. `allow` records that the
workflow accepts this on purpose — for example a screen whose negative verdict blocks and whose unsure
verdict goes to a human — so the choice is reviewed in the YAML instead of silenced.

Changing an agent's engine to or from `decide` in Insight keeps its contract and re-checks it against
the workflow's pinned fixtures ([Insight](insight.md)).
