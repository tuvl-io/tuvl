---
name: tuvl-harden
description: "Change a working workflow safely: pinned fixtures as the safety net, replay before and after."
---
# Hardening and changing a working workflow

1. Before changing anything, pin representative runs: `tuvl list runs --workflow <wf> --json`, then
   `tuvl pin run <run_id> --name <case>` for the happy path and each important branch.
2. Make the change (new engine for an agent, a rule, a prompt version, a model swap).
3. `tuvl replay runs tests/fixtures/<wf>/*.journal.json --json` — every replay must stay `SAME`
   (end and output). A `DIFF` is either a regression to fix or an intended change: then re-pin.
4. `tuvl lint project --strict`, `tuvl run tests`, `tuvl generate lock` (prompts, models, artifacts are pinned).
5. Prefer additive changes: a new artifact version (`@3`) over editing `@2`; `decide.shadow: true`
   to observe a model beside the rules before letting it decide.
- Never delete a fixture to make replays pass.
