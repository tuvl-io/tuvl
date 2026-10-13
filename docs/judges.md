# Judges

A **judge** is a versioned `type: judge` artifact: a model, a rubric and a fixed verdict set, with a
labelled calibration set. One primitive serves output guardrails, tests, loop supervisors and
Insight.

```yaml
kind: Artifact
metadata: { name: refund-reply-quality, version: 2 }
spec:
  type: judge
  model: default                           # AgentModel or LiteLLM id; pinned in tuvl.lock
  rubric: |
    The reply must state the refund amount and timeline and must not promise
    anything outside the refund policy.
  verdict: [pass, fail, uncertain]
  min_score: 0.8                           # a pass scoring below this becomes fail
  calibration:
    - { input: fixtures/judge/reply_ok.json,          expect: pass }
    - { input: fixtures/judge/reply_overpromise.json, expect: fail }
  min_agreement: 0.9
```

A verdict is always `{ verdict, score, reasons }`.

## Where judges run

| Site | On `fail` | On `uncertain` or judge unavailable |
|---|---|---|
| `guardrails.output` (llm, loop) | `guardrail_violation` | **Fails closed:** `error` (`judge_unavailable` / `judge_uncertain`) |
| `Test.expect.judge` | The test fails | The test fails (`tuvl test --allow-uncertain` relaxes it) |
| Loop `supervisor.judge` | `on_violation` (pause / abort / steer) | `on_judge_error` (ignore / pause / abort); rules still apply |

```yaml
# in a Test
expect:
  judge:
    - { judge: artifact://refund-reply-quality@2, target: "{{ text }}" }
```

## Rules

1. **Deterministic checks first.** `end`, `path`, `decisions`, `output` and TEL `assertions` run
   before any judge. Prefer a TEL assertion over a judge wherever the check is exact.
2. **Verdict cache.** Verdicts are cached in `.tuvl/judge-cache/` by subject, judge version and
   content hash, so CI re-runs are deterministic and free. Commit the cache to share it.
3. **Calibration.** `tuvl test` runs each judge's calibration set before first use. Agreement below
   `min_agreement` marks its verdicts untrusted (V023 warning; `tuvl test --strict` fails).
4. **Pinned.** The judge's model is recorded in `tuvl.lock`.

## Building the calibration set in Insight

On the Artifacts page, a judge opens in an editor: model, rubric and thresholds, a **Try** tab and a
**Calibration** tab.

1. Paste a subject (JSON) and **Ask the judge** — this calls the model live, using the unsaved draft.
2. Mark the verdict **✓ right** or **✗ wrong** with a label: the subject is saved to
   `fixtures/judge/<label>.json` and appended to `calibration` with the expected verdict.
3. **Run calibration** to see agreement against `min_agreement`.

Dev API: `POST /dev/judges/try`, `/dev/judges/label`, `/dev/judges/calibrate`.
