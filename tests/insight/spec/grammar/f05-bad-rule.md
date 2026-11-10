---
name: f05
expect: [S015]
incident: "Rule expressions are compiled and type-checked at spec level"
---
# Data
- **Refund** (new) — `amount` (numeric; required)

# Flows
## Flow: route
Trigger: POST /route · signed-in
Input: `amount` (float), `reason` (str)
Ends: default 200 · failed 500

1. `check_amount` — decide rules · in `amount` · out `lane` (enum[auto, review])
   - `amount <= "100"` → auto
   - `reason == 'x'` → review
   - otherwise → manual
   - auto → END
   - review → END
