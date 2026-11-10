---
name: agent-human
description: "Implement a human agent: an approval or review step with a form, routing on a field, groups and expiry."
---
# human agents

```yaml
- id: review
  engine: human
  inputs: { subject: str, summary: str }
  outputs: { approved: bool, note: "str?" }
  human:
    title: "Approve refund: {{ subject }}"
    instruction: Approve if the order is not flagged.
    show: [subject, summary]            # what the reviewer sees — never secure fields
    required_group: support_leads       # who may decide (no self-approval)
    expires: 4h
    route_on: approved                  # bool → routes true/false; enum → one route per value
  routes: { "true": issue, "false": END.denied, expired: END.expired, error: END.failed }
```
- The run suspends (`waiting_human`) and resumes when someone decides via `POST /api/approvals/{id}`
  (scope `approvals:decide`) or Insight. `expired` is required with `expires`.
- Quote `"true"`/`"false"` route keys in YAML.
- `policy.require_human_before: [write]` forces a human before every write on the path.
- Tests: an unmocked human agent suspends the run — assert it with `expect: { wait: human }`, or
  mock it: `mocks: { review: { outputs: { approved: true }, signal: "true" } }`.
