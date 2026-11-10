---
name: f08
expect: [S001, S007, S013]
incident: "Unparseable lines are findings, never skipped; unknown models; human step without group/expiry"
---
# Data
- **Refund** (new) — `order_id` (uuid → Order; required), `amount` (money; required)

# Flows
## Flow: approve
Trigger: POST /approve · signed-in
Input: `amount` (float)
Ends: default 200 · failed 500

1. `ask_lead` — human · in `amount` · out `approved` (bool)
   - true → END
   - false → END
   Please also notify finance when this happens.
