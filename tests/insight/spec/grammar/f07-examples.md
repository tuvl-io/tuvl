---
name: f07
expect: [S012, T001]
incident: "Examples must match the flow; deterministic steps are not mocked"
---
# Data
- **Order** (new) — `total` (numeric; required)

# Flows
## Flow: get_order
Trigger: POST /orders/get · signed-in
Input: `order_id` (uuid)
Ends: default 200 · missing 404 · failed 500

1. `load_order` — tool db read Order · in `order_id` · out `order` (Order)
   - not_found → END.missing

# Acceptance
```tuvl-example
name: mocks a database read and an unknown step
input: { order_id: "6f1c0a52-2a8e-4d0b-9a77-0b9d7c2f1e10" }
mocks:
  load_order: { outputs: { order: { total: 1 }, extra: 1 } }
  fetch_price: { outputs: { price: 1 } }
expect: { end: shipped }
```
