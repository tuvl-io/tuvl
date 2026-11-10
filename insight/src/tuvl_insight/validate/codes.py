"""Validation rule codes (spec Appendix B): what each ``V0xx`` means.

Generated from the specification table; ``tuvl serve mcp`` ``explain_error`` and the
skills read it. Severity: E error, W warning, ``E (ship)`` blocks ``tuvl ship``.
"""

from __future__ import annotations

RULES: dict[str, tuple[str, str]] = {
    "V001": ("E", "Unknown kind, engine, field or artifact type; missing `version: tuvl/v2`"),
    "V002": ("E", "Duplicate agent `id`; invalid id format"),
    "V003": (
        "E",
        "Route target is not an agent, `END`, or a declared `END.<name>`; off-spine agent used as a route target",
    ),
    "V004": (
        "E",
        "An emittable signal (normal or reserved, Appendix C) is unmapped, or a route maps a signal the agent never emits. **W** for an unmapped `error` (the run then ends as failed — consistent with §10.2)",
    ),
    "V005": ("W", "Unreachable agent; cycle without a bounded/human guard"),
    "V006": (
        "E",
        "An input is not produced on every path to the agent (trigger input or upstream outputs)",
    ),
    "V007": ("E", "Producer output type is incompatible with consumer input type"),
    "V008": (
        "E",
        "A template or TEL expression references an undeclared input, or fails to type-check",
    ),
    "V009": ("W", "Two agents on one path write the same key"),
    "V010": ("W / E (ship)", "`pending` agent present"),
    "V011": ("E", "Missing required budget fields for the engine (§13.2)"),
    "V012": (
        "E",
        "`decide`: output enum ≠ normal route keys; rule `then` not in enum; model set without `low_confidence` route. W: model set without `error` route",
    ),
    "V013": (
        "W",
        "`decide` with model and no rules feeds a write/destructive effect without `human`/approval/`compensate` (per `policy.model_only_decisions`)",
    ),
    "V014": (
        "E",
        "Loop tool source unresolved; MCP tool without `allow`; nesting depth exceeds `max_depth`. W: MCP schema differs from lock",
    ),
    "V015": (
        "W",
        "Write/destructive tool or MCP call without `compensate` when a normal route continues to another agent (a step whose normal routes all end the run has nothing after it to fail; for loops, without approval); declared effect weaker than derived. `code` agents are not checked: their writes commit inside their own step's transaction",
    ),
    "V016": (
        "E",
        "`human`: form fields ≠ outputs; `route_on` field is not bool/enum; `expires` set without `expired` route",
    ),
    "V017": ("E", "Artifact ref unresolved or type-incompatible. W: unpinned ref"),
    "V018": (
        "E",
        "Model used by `tool: db` or `ctx.db` is not in `spec.models`; pinned model version disabled",
    ),
    "V019": (
        "E",
        "`public: true` with scope/group; invalid cron; scheduled input that doesn't match `trigger.input`; MCP export without description",
    ),
    "V020": (
        "E",
        "Policy violation (worst-case tokens, bounded-agent cap, human-before-destructive, secure field to LLM)",
    ),
    "V021": ("E", "`transaction: per_run` with human/approval/async"),
    "V022": (
        "E",
        "End body references a key not produced on every path to that end; undeclared end used",
    ),
    "V023": ("W", "Judge calibration agreement below `min_agreement` (test time)"),
    "V024": ("E (ship)", "`tuvl.lock` missing or out of date"),
    "V025": (
        "E",
        "`code` agent `run` not registered, or its signature doesn't match the generated schema (`generate code --check`)",
    ),
    "V026": (
        "E",
        "Decision provider not installed, or an unpinned hosted decision model (W)",
    ),
    "V027": (
        "E (ship)",
        "The image build context isn't the tuvl allowlist, or would contain development files "
        "(specs, task lists, tests, agent rules and skills, compose files, Test/TaskPlan documents)",
    ),
    "V028": (
        "E",
        "A model reference (`llm`, `loop`, `decide.model.ref`) is not a defined AgentModel or a provider/model id",
    ),
    "V029": (
        "W / E (ship)",
        "A model's credentials aren't declared: literal api_key, or an unknown provider without api_key",
    ),
    "V030": ("E", "A `tool: storage` agent names a store that no `kind: Storage` file declares"),
    "V031": ("E", "A write op (`put`, `copy`, `delete`, `presign` PUT) targets a `readonly` store"),
    "V032": (
        "E",
        "A literal storage key is unsafe: absolute, `..`, empty segment, control character",
    ),
    "V033": ("E", "`presign.expires_in` exceeds the store's `presign_max_seconds`"),
    "V034": (
        "E",
        "A `kind: Storage` file is invalid: unknown provider, missing bucket/account/container, "
        "a literal secret instead of `${ENV}`, or an endpoint that is not https",
    ),
    "V035": (
        "E",
        "`map`: `over` is not a list input; the workflow is unknown, is this workflow, or lacks the `as`/`with` inputs; "
        "a required child input is not supplied",
    ),
    "V039": (
        "E",
        "`llm.stream_tokens`: the field to stream is not a `str` output of the agent (or is ambiguous: name it with "
        "`stream_field`), or the agent has `guardrails.output`, which could refuse text already shown",
    ),
    "V038": (
        "E / W",
        "A webhook's `stages` names an agent the workflow does not have (E); `allow_private` lets it "
        "reach internal addresses and plain http (W)",
    ),
    "V037": (
        "E / W",
        "File uploads: `http.upload` without a `file` input, an unknown, read-only or too-small store, "
        "a `file` nested deeper than `file` / `list[file]`, or a `file` input with MCP export (E); "
        "a `file` input on a route with no `upload`, or a public upload (W)",
    ),
    "V043": (
        "E / W",
        "`memory`: a key templated from input on a public trigger (E: anyone can name the owner); an unknown collection (E); "
        "`write` names a field the agent does not have, or a field that carries secure model fields (E)",
    ),
    "V040": (
        "E",
        "`parallel`: a branch is not read-only (a write or destructive tool, a code agent with a write effect, an MCP tool)",
    ),
    "V041": (
        "E",
        "`parallel`: a branch reads an input the step does not take, two branches produce the same output, "
        "or the step's outputs are not exactly the branches' outputs (optional with `on_error: collect`)",
    ),
    "V042": (
        "E",
        "`parallel` under `transaction: per_run` (its branches use sessions of their own)",
    ),
    "V045": (
        "E",
        "`EnvConfig`: malformed document, a variable declared twice, an invalid name, or a secret / required variable that also has a default",
    ),
    "V046": (
        "E",
        "A `${VAR}` reference (or `os.environ` read) names a variable no `EnvConfig` declares. Applies once the project has an `EnvConfig`",
    ),
    "V047": ("W", "An `EnvConfig` variable that nothing in the project refers to"),
    "V044": (
        "E",
        "`RoleSeed`: malformed document, a role listed twice (in one file or across files), or an invalid role name or scope",
    ),
    "V036": (
        "E",
        "`map`: the mapped workflow has a `human` or `pending` agent, or a loop tool that needs approval, so a run could wait",
    ),
}
