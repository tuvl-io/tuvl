---
name: f01
expect: [S004, S003]
incident: "Report #2 — a spec'd 404 end was unreachable"
---
# Data
- **Order** (new) — `total` (numeric; required)

# Flows
## Flow: get_order
Trigger: POST /orders/get · signed-in
Input: `order_id` (uuid)
Ends: default 200 · unknown_order 404 · failed 500

1. `load_order` — tool db read Order · in `order_id` · out `order` (Order)
