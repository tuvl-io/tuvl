---
name: agent-decide
description: "Implement a decide agent: ordered TEL rules first, an optional decision model with a confidence threshold, shadow mode, failure semantics."
---
# decide agents

```yaml
- id: route
  engine: decide
  inputs: { amount: float, reason: "enum[damaged, other]" }
  outputs: { lane: "enum[auto, manual, deny]" }      # exactly one enum: its values are the routes
  decide:
    rules:                                           # first match wins; the model is not called
      - { when: "amount > 500", then: manual }
      - { when: "reason == 'damaged'", then: auto }
    model: { ref: triage-classifier, min_confidence: 0.7 }   # optional: AgentModel type decision|llm
  routes: { auto: issue, manual: review, deny: END.denied, low_confidence: review, error: END.failed }
```
- Prefer rules: they are deterministic, free and explainable. Add a final `when: "true"` catch-all
  when there is no model.
- With a model: `low_confidence` is required; a model outage routes `error` — never a silent default.
- `shadow: true` runs the model beside the rules and journals both, while the rules decide.
- Every decision is journaled (`source: rule#N | model`); test it with
  `expect: { decisions: { route: { route: manual, source: "rule#1" } } }`.
- A model-only decide feeding writes is warned (V013) — add rules or a human.
