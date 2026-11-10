"""The starting point of a new spec."""

SPEC_TEMPLATE = """---
name: {name}
owner: {owner}
workflow: {workflow}
---
# Intent
What this automates and why, in a few sentences. Prose sections are for people and the coding
agent; `tuvl lint spec` checks # Data, # Flows and the examples (grammar: docs/spec-driven.md).

# Data
- **Item** (new) — `title` (string; required), `status` (enum: open | closed; default open)

# Flows

## Flow: {workflow}
Trigger: POST /{name} · signed-in
Input: `title` (str)
Ends: default 201 · failed 500

1. `save` — tool db create Item · in `title` · out `item` (Item)
   - conflict → END.failed

# Policies
Rules that must always hold (prose).

# Acceptance
Each example becomes a test (`tuvl generate code`). Rename the fence below to `tuvl-example` once the
flow is real:

```yaml
name: an item is saved
input: {{ title: First }}
expect: {{ end: default }}
```
"""
