---
name: tuvl-data
description: "Define and change data models (ModelDefinition): fields, secure PII, access scopes and per-operation CRUD."
---
# Data models

```yaml
kind: ModelDefinition
version: v1
metadata: { name: Refund, schema_version: v1, description: A refund issued for an order. }
spec:
  tablename: refunds
  api: { crud: [list, read] }          # off by default; each listed operation is IAM-guarded
  access: { read_groups: [support] }   # optional group gates on top of scopes
  fields:
    - { name: id, type: uuid, primary_key: true, default: uuid4, input: false }
    - { name: order_id, type: uuid, required: true, index: true }
    - { name: amount, type: numeric, required: true }
    - { name: status, type: enum, enum_values: [pending, paid], required: true }
    - { name: customer_email, type: string, secure: true }     # PII: redacted, never to models
    - { name: created_at, type: timestamptz, input: false }
```
- Field types: `string text integer bigint numeric float boolean uuid date timestamptz jsonb enum`.
- In contracts the model is a type: `Refund` (read), `Refund.create`, `Refund.update`, `list[Refund]`.
- Workflows list the models their agents touch in `spec.models`; writes go through workflows
  (journaled, contract-checked), not CRUD.
- After changing a model: `tuvl lint project`, `tuvl generate code`, `tuvl generate lock`; row-level security comes from
  `tuvl generate rls` (check it with `tuvl verify rls`) — never edit tables by hand.
