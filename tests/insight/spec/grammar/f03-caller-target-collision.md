---
name: f03
expect: [S009]
incident: "caller and target both named user_id — a role change could hit the caller"
---
# Data
- **Membership** (new) — `user_id` (string; required), `role` (enum: manager | member; required)

# Flows
## Flow: change_role
Trigger: POST /roles · group admins
Input: `user_id` (str), `role` (enum[manager, member])
Ends: default 200 · failed 500

1. `identify_caller` — code · effect read · out `user_id` (str)
2. `update_role` — code · effect write · in `user_id`, `role`
