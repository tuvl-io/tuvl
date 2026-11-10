---
name: f04
expect: [S008, S010]
incident: "Report #6 — PII flag and access gate lost; secure data sent to a model"
---
# Data
- **Ticket** (new) — `customer_email` (string; required; secure), `body` (text)

# Flows
## Flow: triage
Trigger: POST /triage
Input: `customer_email` (str), `body` (str)
Ends: default 200 · failed 500

1. `classify` — llm default · in `customer_email`, `body` · out `category` (enum[billing, other]) · budget 500 tok, 10s
   - parse_error, timeout, budget_exceeded → END
