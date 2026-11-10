---
name: refund-request
owner: payments-team
---
# Intent
Customers request refunds for an order. Small refunds are paid automatically; large ones need a
finance lead. Every request is recorded with its outcome, and the customer's email never reaches a
model.

# Data
- **Order** (existing) — `id` (uuid), `customer_email` (string; secure), `total` (numeric),
  `status` (string)
- **Refund** (new) — `order_id` (uuid → Order; required), `amount` (numeric; required),
  `reason` (text; required), `category` (enum: damaged | late | wrong_item | other),
  `status` (enum: requested | approved | rejected | paid; required; default requested),
  `payment_ref` (string)

# Flows

## Flow: refund_request
Trigger: POST /refunds · signed-in
Input: `order_id` (uuid), `amount` (float), `reason` (str)
Ends: default 201 · unknown_order 404 · rejected 200 · unavailable 503 · failed 500
Policy: max 2000 tok per run

1. `load_order` — tool db read Order · in `order_id` · out `order` (Order)
   - not_found → END.unknown_order
2. `classify_reason` — llm default · in `reason` · out `category` (enum[damaged, late, wrong_item, other]) · budget 800 tok, 20s
   - parse_error, timeout, budget_exceeded → `check_amount`
   - error → END.unavailable
3. `check_amount` — decide rules · in `amount` · out `lane` (enum[auto, review])
   - `amount <= 100` → auto
   - otherwise → review
   - auto → `record_refund`
   - review → `finance_review`
4. `finance_review` — human group finance_lead · expires 24h · in `order`, `amount`, `reason` · out `approved` (bool)
   - true → `record_refund`
   - false → END.rejected
   - expired → END.rejected
5. `record_refund` — tool db create Refund · in `order_id`, `amount`, `reason` · out `refund` (Refund)
   - conflict → END.failed
6. `pay_refund` — tool http POST payments · effect write · in `refund` · out `payment_ref` (str)
   - error → END.unavailable

# Policies
Refunds above 100 always need a finance lead. The payments call is idempotent per refund.

# Acceptance
```tuvl-example
name: a small refund is paid automatically
given:
  Order: [{ id: "6f1c0a52-2a8e-4d0b-9a77-0b9d7c2f1e10", customer_email: a@example.com, total: 120, status: delivered }]
as: { user: u-customer }
input: { order_id: "6f1c0a52-2a8e-4d0b-9a77-0b9d7c2f1e10", amount: 40, reason: "The mug arrived broken." }
mocks:
  classify_reason: { outputs: { category: damaged } }
  pay_refund: { outputs: { payment_ref: pay_123 } }
expect: { end: default }
```
```tuvl-example
name: an unknown order is a 404
input: { order_id: "00000000-0000-4000-8000-000000000000", amount: 10, reason: "Late." }
expect: { end: unknown_order }
```
```tuvl-example
name: a large refund waits for a finance lead
given:
  Order: [{ id: "6f1c0a52-2a8e-4d0b-9a77-0b9d7c2f1e11", customer_email: b@example.com, total: 900, status: delivered }]
input: { order_id: "6f1c0a52-2a8e-4d0b-9a77-0b9d7c2f1e11", amount: 500, reason: "Wrong item." }
mocks:
  classify_reason: { outputs: { category: wrong_item } }
expect: { wait: human }
```
```tuvl-example
name: a rejected refund ends as rejected
given:
  Order: [{ id: "6f1c0a52-2a8e-4d0b-9a77-0b9d7c2f1e12", customer_email: c@example.com, total: 900, status: delivered }]
input: { order_id: "6f1c0a52-2a8e-4d0b-9a77-0b9d7c2f1e12", amount: 500, reason: "Changed my mind." }
mocks:
  classify_reason: { outputs: { category: other } }
  finance_review: { signal: "false", outputs: { approved: false } }
expect: { end: rejected }
```
- A refund is never paid twice for the same request.
