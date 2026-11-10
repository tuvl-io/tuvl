---
name: f02
expect: [S005, S004]
incident: "Report #1 — PII screen without a real model contract (threshold, low_confidence)"
---
# Data
- **Post** (new) — `body` (text; required)

# Flows
## Flow: screen
Trigger: POST /screen · signed-in
Input: `body` (str)
Ends: default 200 · blocked 422 · failed 500

1. `screen_pii` — decide model laya · in `body` · out `verdict` (enum[clean, contains_pii])
   - clean → END
   - contains_pii → END.blocked
