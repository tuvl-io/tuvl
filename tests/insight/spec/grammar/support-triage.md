---
name: support-triage
owner: support-team
---
# Intent
Classify incoming support tickets, route them, and let an investigator resolve billing problems —
crediting the customer when it helps, with a team lead's approval above 50.

# Data
- **SupportTicket** (existing) — `subject` (string), `body` (text), `category` (string),
  `urgency` (string), `resolution` (text)
- **AccountCredit** (existing) — `customer_id` (string; required), `amount` (numeric; required),
  `reason` (text)

# Flows

## Flow: triage
Trigger: POST /support/triage · signed-in
Input: `customer_id` (str), `subject` (str), `body` (str)
Ends: default 201 · failed 502

1. `classify` — llm default · in `subject`, `body` · out `category` (enum[billing, technical, account]), `urgency` (enum[low, medium, high]) · budget 300 tok, 60s, retry 2
   - parse_error, timeout, budget_exceeded → END.failed
2. `route` — decide rules + model triage-classifier ≥ 0.7 · in `category`, `urgency`, `subject`, `body` · out `route` (enum[investigate, store]) · budget 20s, retry 2
   - `category == "technical"` → store
   - `category == "billing" and urgency == "high"` → investigate
   - investigate → `investigate`
   - store → `persist`
   - low_confidence → `investigate`
3. `investigate` — loop default · in `customer_id`, `subject`, `body` · out `resolution` (str) · budget 20000 tok, 3m, 6 iterations, 4 tool calls · guardrails `no-secrets@1`
   · tools: `lookup_account`, workflow `issue_credit` (approval team_leads when `args.amount > 50`; max 1 call)
   - resolved, needs_human → `persist`
   - max_iterations, budget_exceeded, timeout, aborted, guardrail_violation → `persist`
4. `persist` — tool db create SupportTicket · in `subject`, `body`, `category`, `urgency`, `resolution`? · out `ticket` (SupportTicket)
   - conflict → END.failed

T1. `lookup_account` — code · effect read · in `customer_id` · out `account` (json) · tool of `investigate`

## Flow: issue_credit
Trigger: tool
Input: `customer_id` (str), `amount` (int), `reason` (str)
Ends: default 201 · failed 502

1. `credit` — tool db create AccountCredit · in `customer_id`, `amount`, `reason` · out `credit` (AccountCredit)
   - conflict → END.failed

# Acceptance
```tuvl-example
name: a technical ticket is stored without investigation
workflow: triage
input: { customer_id: c-1, subject: "App crashes", body: "It crashes on start." }
mocks:
  classify: { outputs: { category: technical, urgency: low } }
expect: { end: default, path: [classify, route, persist] }
```
```tuvl-example
name: an urgent billing ticket is investigated
workflow: triage
input: { customer_id: c-2, subject: "Double charge", body: "I was charged twice." }
mocks:
  classify: { outputs: { category: billing, urgency: high } }
  investigate: { signal: resolved, outputs: { resolution: "Refunded the duplicate charge." } }
expect: { end: default, path_includes: [investigate] }
```
```tuvl-example
name: a credit is recorded
workflow: issue_credit
input: { customer_id: c-2, amount: 20, reason: goodwill }
expect: { end: default }
```
