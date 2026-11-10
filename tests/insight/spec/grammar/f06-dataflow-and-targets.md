---
name: f06
expect: [S006, S002]
incident: "A step reads data no earlier step produces; a route targets an undeclared end"
---
# Data
- **Order** (new) — `total` (numeric; required)

# Flows
## Flow: pay
Trigger: POST /pay · signed-in
Input: `order_id` (uuid)
Ends: default 200 · failed 500

1. `charge` — tool http POST payments · effect write · in `order_id`, `amount` · out `payment_ref` (str)
   - error → END.declined
