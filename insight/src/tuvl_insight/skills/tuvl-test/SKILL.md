---
name: tuvl-test
description: "Write and run tuvl tests: Test documents, mocks, replayed fixtures, judges and calibration."
---
# Tests

```yaml
kind: Test
version: tuvl/v2
metadata: { name: large-refund-needs-a-manager }
spec:
  workflow: refund_flow
  given:                                        # rows that exist before the run
    Order: [{ id: "…", total: 900, status: delivered }]
  as: { user: u-customer }                      # the caller (ctx.principal)
  input: { order_id: "…", reason: damaged }
  mocks:                                        # model / external / human steps only
    classify: { outputs: { category: damaged } }
  replay: tests/fixtures/refund_flow/large.journal.json   # or serve results from a pinned run
  expect:
    wait: human                                 # or end: <name>; not both
    path_includes: [triage]
    decisions: { triage: { source: "rule#1" } }
    output: { status: 202 }                     # partial match
    assertions: ["output.status == 202"]        # TEL over output, end and the context
    judge: [{ judge: artifact://reply-quality@1, target: "{{ output.body.reply }}" }]
```
- `tuvl run tests [--json] [--only name] [--live] [--allow-uncertain] [--strict]`. Offline by default:
  unmocked llm/loop/http/mcp/storage/model-decide agents fail with a pointer; `db` tools and `code` agents
  run for real against an in-memory database seeded by `given:`; unmocked human agents suspend.
  That database autocommits, so it keeps writes Postgres would roll back (an agent's `error` outcome, a retried
  attempt, a `transaction: per_run` rollback): a test cannot prove a rollback, check it against Postgres.
- Results are **PASS**, **STUBBED** or **FAIL**. STUBBED means the expectations held only because
  the run went through unmocked `pending` placeholders — it is not a pass, task status ignores it
  and `--strict` (the CI gate) fails it. Implement the pending agent; don't mock it away.
- Tests generated from spec examples (`metadata.generated_from`) are owned by `tuvl generate code` —
  change the spec, not the file. Hand-written tests live beside them.
- Pin a real run: `tuvl pin run <run_id>` → `tests/fixtures/…`; check equivalence after an engine
  change with `tuvl replay runs <fixture>`.
- **Mock policy:** mock only what is non-deterministic, external or human — `llm`, `loop`, `decide`
  with a model, `tool http`/`mcp`/`storage`, `human`. For a `map`, mock the child workflow's agents by id in the parent's test. Business logic (`code`, `tool db`, `decide` rules) must run:
  seed the rows it reads with `given:` (model name → list of rows) and the caller with `as:`. Mocking
  it prints warning **T001**. To test a "not found" path, simply don't seed the row.

## Judges
`artifacts/<name>.yaml` (structured artifact; reference it as `artifact://<name>@1`):
```yaml
kind: Artifact
metadata: { name: reply-quality, version: 1, description: The reply states the amount and promises nothing extra. }
spec:
  type: judge
  model: default                   # an AgentModel
  rubric: |
    Pass when the reply states the refunded amount and promises nothing beyond policy. Otherwise fail.
  verdict: [pass, fail, uncertain]
  min_score: 0.6
  calibration:                     # labelled examples, paths relative to the project
    - { input: fixtures/judge/good-reply.json, expect: pass }
    - { input: fixtures/judge/overpromise.json, expect: fail }
  min_agreement: 0.9
```
- A calibration fixture is a JSON file holding exactly what the judge will see (e.g. `"Refunded $40."`
  or an object). `tuvl run tests` calibrates every judge it uses first; below `min_agreement` the judge is
  untrusted (V023, `--strict` fails). Verdicts are cached in `.tuvl/judge-cache/` (deterministic CI).
- In a Test: `judge: [{ judge: artifact://reply-quality@1, target: "{{ output.body.reply }}" }]`.
- As a guardrail on an llm/loop agent: `guardrails: { output: [artifact://reply-quality@1] }` — a fail
  routes `guardrail_violation`.
