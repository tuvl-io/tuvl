"""tuvl 2.0 document schema (spec §7–§13, §17.4, §18.1).

One Pydantic model per document shape. Agents are a discriminated union on
``engine``; each engine's parameters live under a key named after the engine,
and every model forbids unknown keys, so ``engine: llm`` with a ``tool:``
block is a schema error, not a silently ignored field.

This module checks *shape*: field presence, closed vocabularies, value
ranges, and the syntax of embedded types (§6.1), TEL conditions (§6.2) and
cron expressions. Cross-references — route targets, data flow, budgets per
engine, artifact resolution — are the validator's job (``tuvl_insight.validate``),
where they get rule codes and file locations.

``workflow_json_schema()`` is the artefact Insight renders its forms from.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from tuvl.contract.signals import is_reserved
from tuvl.contract.tel import TELSyntaxError, compile_expr
from tuvl.contract.types import TypeParseError, parse_field

SCHEMA_VERSION = "tuvl/v2"

# ── Primitive string types ───────────────────────────────────────────────────

AgentId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
WorkflowName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
EndName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
Signal = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$", max_length=64)]
#: An agent id, ``END`` (= ``END.default``) or ``END.<output name>``.
RouteTarget = Annotated[
    str, StringConstraints(pattern=r"^(END(\.[a-z][a-z0-9_]*)?|[a-z][a-z0-9_]*)$")
]
ModelName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Za-z0-9_]*$")]
#: A model allow-list entry, optionally pinned: ``Order`` or ``Order@v2``.
ModelPin = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Za-z0-9_]*(@[A-Za-z0-9_.\-]+)?$")]
ArtifactRef = Annotated[
    str, StringConstraints(pattern=r"^artifact://[A-Za-z0-9][A-Za-z0-9_.\-]*(@[1-9][0-9]*)?$")
]
#: ``30s``, ``1.5m``, ``250ms``, ``48h``, ``7d``.
Duration = Annotated[str, StringConstraints(pattern=r"^\d+(\.\d+)?(ms|s|m|h|d)$")]
#: A ``code`` runner name: ``issue_refund`` or a dotted built-in like ``tuvl.data_search``.
RunnerName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
]
Effect = Literal["read", "write", "destructive"]

_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d)$")


def duration_seconds(value: str) -> float:
    """``"1.5m"`` → ``90.0``."""
    m = _DURATION.match(value)
    if not m:
        raise ValueError(f"invalid duration {value!r}")
    return float(m.group(1)) * _DURATION_UNITS[m.group(2)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_tel(text: str) -> str:
    try:
        compile_expr(text)
    except TELSyntaxError as exc:
        raise ValueError(str(exc)) from exc
    return text


TelCondition = Annotated[
    str, Field(description="A TEL expression that must evaluate to a bool (§6.2).")
]


# ── Typed field maps (§6.1) ──────────────────────────────────────────────────


class LongFormField(_Strict):
    type: str
    description: str | None = None
    default: JsonValue = None


FieldMap = dict[str, str | LongFormField]


def _union_outputs(per_signal: dict[str, Any]) -> dict[str, Any]:
    """Merge per-signal field maps; a field missing on some signal becomes
    optional. The same field must have the same type on every signal."""
    from tuvl.contract.types import format_type

    seen: dict[str, Any] = {}
    counts: dict[str, int] = {}
    for signal, fields in per_signal.items():
        for name, spec in (fields or {}).items():
            raw = spec.get("type") if isinstance(spec, dict) else spec
            if name in seen and seen[name] != raw:
                raise ValueError(
                    f"output '{name}' is {seen[name]!r} on one signal and {raw!r} on "
                    f"'{signal}' — a field has one type on every signal"
                )
            seen[name] = raw
            counts[name] = counts.get(name, 0) + 1
    merged: dict[str, Any] = {}
    for name, raw in seen.items():
        if counts[name] < len(per_signal) and isinstance(raw, str):
            try:
                ref = parse_field(raw).type
            except TypeParseError:
                merged[name] = raw
                continue
            merged[name] = format_type(ref) if ref.optional else format_type(ref) + "?"
        else:
            merged[name] = raw
    return merged


def _check_field_map(value: dict[str, Any]) -> dict[str, Any]:
    for name, spec in value.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid field name {name!r}")
        raw = spec.model_dump(exclude_unset=True) if isinstance(spec, LongFormField) else spec
        try:
            parse_field(raw)
        except TypeParseError as exc:
            raise ValueError(f"{name}: {exc}") from exc
    return value


# ── Agent contract (§8) ──────────────────────────────────────────────────────


RetryError = Literal["timeout", "rate_limit", "parse_error", "transient"]


class RetryPolicy(_Strict):
    attempts: int = Field(ge=1, le=10)
    errors: list[RetryError] = ["timeout", "rate_limit", "transient"]
    backoff: float = Field(default=1.5, ge=1.0, le=10.0)


class Budget(_Strict):
    timeout: Duration | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_iterations: int | None = Field(default=None, ge=1, le=100)
    max_tool_calls: int | None = Field(default=None, ge=1, le=1000)
    retry: RetryPolicy | None = None


class Guardrails(_Strict):
    input: list[ArtifactRef] = []
    output: list[ArtifactRef] = []
    tools: list[ArtifactRef] = []


class _AgentBase(_Strict):
    id: AgentId
    description: str = Field(min_length=1)
    inputs: FieldMap = {}
    #: Every field the agent can output. With per-signal outputs this is the
    #: union — fields not produced on every signal are optional here.
    outputs: FieldMap = {}
    #: Per-signal outputs (Q10): ``outputs`` written as ``{signal: {field: type}}``.
    #: A normal signal that is not listed produces no outputs.
    signal_outputs: dict[Signal, FieldMap] | None = None
    budget: Budget = Budget()
    routes: dict[Signal, RouteTarget]
    hooks: list[ArtifactRef] = []
    compensate: AgentId | None = None

    _fields_ok = field_validator("inputs", "outputs")(_check_field_map)

    @model_validator(mode="before")
    @classmethod
    def _per_signal_outputs(cls, data: Any) -> Any:
        """``outputs`` keyed by this agent's routed signals is the per-signal
        form: every key a non-reserved route signal, every value a field map."""
        if not isinstance(data, dict) or "signal_outputs" in data:
            return data
        outputs, routes = data.get("outputs"), data.get("routes")
        if not (isinstance(outputs, dict) and outputs and isinstance(routes, dict)):
            return data
        signals = {str(k).lower() if isinstance(k, bool) else str(k) for k in routes}
        normal = {k for k in signals if not is_reserved(k)}
        if not (set(outputs) <= normal and all(isinstance(v, dict) for v in outputs.values())):
            return data
        return {**data, "signal_outputs": outputs, "outputs": _union_outputs(outputs)}

    @field_validator("signal_outputs")
    @classmethod
    def _signal_fields_ok(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            for fields in value.values():
                _check_field_map(fields)
        return value

    def outputs_for(self, signal: str) -> dict[str, Any]:
        """The fields produced when the agent emits *signal* (a normal signal)."""
        if self.signal_outputs is not None:
            return dict(self.signal_outputs.get(signal, {}))
        return dict(self.outputs)

    def output_type(self, key: str) -> str | LongFormField | None:
        """The declared type of *key* on the signals that produce it."""
        if self.signal_outputs is not None:
            for fields in self.signal_outputs.values():
                if key in fields:
                    return fields[key]
            return None
        return self.outputs.get(key)

    @field_validator("routes", mode="before")
    @classmethod
    def _bool_route_keys(cls, value: Any) -> Any:
        # YAML 1.1 reads unquoted `true:`/`false:` keys as booleans.
        if isinstance(value, dict):
            return {(str(k).lower() if isinstance(k, bool) else k): v for k, v in value.items()}
        return value


class PendingAgent(_AgentBase):
    engine: Literal["pending"]


class CodeParams(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run: RunnerName
    #: Static parameters (templates rendered against the inputs) → ``ctx.params``.
    with_: dict[str, JsonValue] = Field(default={}, alias="with")


class CodeAgent(_AgentBase):
    engine: Literal["code"]
    code: CodeParams
    effect: Effect = "write"


# tool (§9.3)


class DbTool(_Strict):
    use: Literal["db"]
    model: ModelName
    op: Literal["create", "read", "update", "delete", "list", "upsert"]
    where: dict[str, JsonValue] | None = None
    values: dict[str, JsonValue] | None = None
    order_by: list[str] | None = None
    limit: int | None = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def _op_fields(self) -> DbTool:
        need_where = {"read", "update", "delete"}
        need_values = {"create", "update", "upsert"}
        if self.op in need_where and not self.where:
            raise ValueError(f"op '{self.op}' needs 'where'")
        if self.op in need_values and not self.values:
            raise ValueError(f"op '{self.op}' needs 'values'")
        if self.op not in need_values and self.values is not None:
            raise ValueError(f"op '{self.op}' does not take 'values'")
        if self.op != "list" and (self.order_by is not None or self.limit is not None):
            raise ValueError("'order_by' and 'limit' only apply to op 'list'")
        return self


class HttpExpect(_Strict):
    status: list[Annotated[int, Field(ge=100, le=599)]] = Field(
        default_factory=lambda: [200, 201, 202, 204]
    )


class HttpTool(_Strict):
    use: Literal["http"]
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    url: str = Field(min_length=1)
    headers: dict[str, str] = {}
    query: dict[str, JsonValue] = {}
    body: JsonValue = None
    expect: HttpExpect = HttpExpect()
    map: dict[str, str] = Field(default={}, description="Declared output key → response path.")
    idempotency: Literal["auto", "none"] = "none"

    @model_validator(mode="after")
    def _no_body_on_safe_methods(self) -> HttpTool:
        if self.method in ("GET", "HEAD") and self.body is not None:
            raise ValueError(f"{self.method} requests do not take a body")
        return self


class McpTool(_Strict):
    use: Literal["mcp"]
    server: ArtifactRef
    name: str = Field(min_length=1)
    args: dict[str, JsonValue] = {}


_STORAGE_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    # op → (required, allowed besides store/op/map)
    "put": ({"key", "body"}, {"encoding", "content_type", "metadata", "if_exists"}),
    "get": ({"key"}, {"as_", "max_bytes"}),
    "head": ({"key"}, set()),
    "list": (set(), {"prefix", "limit", "cursor"}),
    "copy": ({"key", "to_key"}, {"to_store", "if_exists"}),
    "delete": ({"key"}, set()),
    "presign": ({"key"}, {"method", "expires_in"}),
}


class StorageTool(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    use: Literal["storage"]
    store: str = Field(min_length=1)
    op: Literal["put", "get", "head", "list", "copy", "delete", "presign"]
    key: str | None = Field(default=None, min_length=1)
    body: JsonValue = None
    encoding: Literal["text", "base64", "json"] = "text"
    content_type: str | None = None
    metadata: dict[str, str] = {}
    if_exists: Literal["fail", "overwrite"] = "fail"
    as_: Literal["text", "json", "base64"] = Field(default="text", alias="as")
    max_bytes: int | None = Field(default=None, ge=1)
    prefix: str | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)
    cursor: str | None = None
    to_key: str | None = Field(default=None, min_length=1)
    to_store: str | None = None
    method: Literal["GET", "PUT"] = "GET"
    expires_in: int | None = Field(default=None, ge=1)
    map: dict[str, str] = Field(default={}, description="Declared output key → result path.")

    @model_validator(mode="after")
    def _op_fields(self) -> StorageTool:
        required, allowed = _STORAGE_FIELDS[self.op]
        given = {f for f in self.model_fields_set if f not in ("use", "store", "op", "map")}
        for name in sorted(required):
            if name not in given or getattr(self, name) is None:
                raise ValueError(f"op '{self.op}' needs '{'as' if name == 'as_' else name}'")
        extra = given - required - allowed
        if extra:
            names = ", ".join(sorted("as" if n == "as_" else n for n in extra))
            raise ValueError(f"op '{self.op}' does not take: {names}")
        return self


class ToolAgent(_AgentBase):
    engine: Literal["tool"]
    tool: Annotated[DbTool | HttpTool | McpTool | StorageTool, Field(discriminator="use")]
    effect: Effect | None = Field(
        default=None,
        description="Override; never weaker than the derived effect, except that an "
        "http POST/PUT/PATCH may declare 'read' (a lookup sent as POST).",
    )


# llm (§9.4)


class MemoryParams(_Strict):
    """What an agent remembers across runs (see the memory built-ins)."""

    #: A ``kind: Collection`` that holds the memories.
    collection: str = Field(min_length=1)
    #: Whose memory: ``principal`` (the signed-in user), ``tenant``, or a template over the agent's inputs.
    key: str = Field(default="principal", min_length=1)
    #: The owner's latest memories handed to the agent.
    window: int = Field(default=5, ge=0, le=50)
    #: Older memories related to the prompt, added to the window.
    recall_top_k: int = Field(default=3, ge=0, le=20)
    #: The most recalled text put in the prompt (about 4 characters a token); the oldest is dropped first.
    max_tokens: int = Field(default=1000, ge=1, le=20000)
    #: The inputs and outputs stored after a successful run; default: every output. ``[]`` only reads.
    write: list[str] | None = None
    #: Memories older than this are deleted by the runtime (``30d``, ``12h``); default: kept until erased.
    retention: str | None = None

    @field_validator("retention")
    @classmethod
    def _retention_is_a_duration(cls, value: str | None) -> str | None:
        if value is not None and duration_seconds(value) <= 0:
            raise ValueError("retention must be a positive duration such as 30d")
        return value


class LlmParams(_Strict):
    model: str = Field(min_length=1)
    system: str | None = None
    prompt: str | None = None
    skills: list[ArtifactRef] = []
    #: Show the answer's ``stream_field`` on the run's event stream while the model writes it.
    stream_tokens: bool = False
    #: The ``str`` output to stream; optional when the agent has exactly one.
    stream_field: str | None = None
    #: Remember across runs: recalled text goes into the prompt, the result is stored afterwards.
    memory: MemoryParams | None = None

    @model_validator(mode="after")
    def _has_instruction(self) -> LlmParams:
        if not (self.system or self.prompt):
            raise ValueError("an llm agent needs 'system' and/or 'prompt'")
        if self.stream_field and not self.stream_tokens:
            raise ValueError("'stream_field' needs 'stream_tokens: true'")
        return self


class LlmAgent(_AgentBase):
    engine: Literal["llm"]
    llm: LlmParams
    guardrails: Guardrails = Guardrails()


# decide (§10)


class DecideRule(_Strict):
    when: TelCondition
    then: Signal

    _tel_ok = field_validator("when")(_check_tel)


class DecideModel(_Strict):
    ref: str = Field(min_length=1, description="AgentModel name (type decision or llm).")
    min_confidence: float = Field(ge=0.0, le=1.0)


class DecideParams(_Strict):
    rules: list[DecideRule] | ArtifactRef = []
    model: DecideModel | None = None
    shadow: bool = False

    @model_validator(mode="after")
    def _can_decide(self) -> DecideParams:
        if not self.rules and self.model is None:
            raise ValueError("decide needs 'rules', a 'model', or both")
        if self.shadow and self.model is None:
            raise ValueError("'shadow' needs a 'model' to run alongside the rules")
        if self.shadow and not self.rules:
            raise ValueError("'shadow' needs 'rules' — in shadow mode the rules decide")
        return self


class DecideAgent(_AgentBase):
    engine: Literal["decide"]
    decide: DecideParams


# loop (§11)


class ApprovalWhen(_Strict):
    when: TelCondition
    required_group: str | None = None

    _tel_ok = field_validator("when")(_check_tel)


Approval = Literal["required"] | ApprovalWhen


class _ToolRefBase(_Strict):
    approval: Approval | None = None
    max_calls: int | None = Field(default=None, ge=1, le=1000)


class AgentToolRef(_ToolRefBase):
    agent: AgentId


class WorkflowToolRef(_ToolRefBase):
    workflow: WorkflowName


class CollectionToolRef(_ToolRefBase):
    """Search a collection: the model supplies the query, ``top_k`` and ``filter`` are fixed here."""

    collection: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=100)
    filter: dict[str, JsonValue] = {}
    scope: Literal["global", "run"] = "global"
    name: AgentId | None = None

    @property
    def tool_name(self) -> str:
        """The name the model sees: ``name``, else ``search_<collection>`` as a valid function name."""
        return self.name or "search_" + re.sub(r"[^a-z0-9_]+", "_", self.collection.lower()).strip(
            "_"
        )


class McpToolRef(_ToolRefBase):
    mcp: ArtifactRef
    allow: list[str] = Field(min_length=1, description="Explicit allow-list of MCP tool names.")


LoopTool = AgentToolRef | WorkflowToolRef | CollectionToolRef | McpToolRef


class SupervisorRule(_Strict):
    when: Literal["tool_repeated", "budget_fraction", "iteration_reached", "effect_count"]
    then: Literal["abort", "pause", "steer"]
    tool: str | None = None
    count: int | None = Field(default=None, ge=1)
    gte: int | None = Field(default=None, ge=1)
    gt: float | None = Field(default=None, gt=0.0, lt=1.0)
    effect: Literal["write", "destructive"] | None = None

    @model_validator(mode="after")
    def _params(self) -> SupervisorRule:
        required = {
            "tool_repeated": ("count",),
            "budget_fraction": ("gt",),
            "iteration_reached": ("gte",),
            "effect_count": ("effect", "count"),
        }[self.when]
        allowed = set(required) | ({"tool"} if self.when == "tool_repeated" else set())
        for name in ("tool", "count", "gte", "gt", "effect"):
            value = getattr(self, name)
            if name in required and value is None:
                raise ValueError(f"rule '{self.when}' needs '{name}'")
            if name not in allowed and value is not None:
                raise ValueError(f"rule '{self.when}' does not take '{name}'")
        return self


class Supervisor(_Strict):
    rules: list[SupervisorRule] = []
    judge: ArtifactRef | None = None
    every_n_iterations: int = Field(default=1, ge=1)
    on_violation: Literal["abort", "pause", "steer"] = "pause"
    on_judge_error: Literal["ignore", "pause", "abort"] = "ignore"
    steer_message: str | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Supervisor:
        if not self.rules and self.judge is None:
            raise ValueError("a supervisor needs 'rules', a 'judge', or both")
        return self


class LoopParams(_Strict):
    model: str = Field(min_length=1)
    steering: str = Field(min_length=1)
    skills: list[ArtifactRef] = []
    tools: list[LoopTool] = Field(min_length=1)
    supervisor: Supervisor | None = None
    #: Remember across runs: recalled text opens the conversation, the result is stored afterwards.
    memory: MemoryParams | None = None


class LoopAgent(_AgentBase):
    engine: Literal["loop"]
    loop: LoopParams
    guardrails: Guardrails = Guardrails()


# human (§9.6)


class HumanFormField(_Strict):
    label: str | None = None
    widget: Literal["text", "textarea", "toggle", "select", "number", "date"] | None = None
    help: str | None = None


class HumanParams(_Strict):
    title: str = Field(min_length=1)
    instruction: str | None = None
    show: list[str] = []
    form: dict[str, HumanFormField] = {}
    required_group: str | None = None
    assignee: str | None = None
    expires: Duration | None = None
    route_on: str | None = None


class HumanAgent(_AgentBase):
    engine: Literal["human"]
    human: HumanParams


# map (fan-out over a list)


class MapParams(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: The input that holds the list to fan out over.
    over: str = Field(min_length=1)
    #: The child workflow run once per item.
    workflow: str = Field(min_length=1)
    #: The child trigger input that receives the item.
    as_: str = Field(default="item", min_length=1, alias="as")
    #: Further child inputs, templates rendered against this agent's inputs.
    with_: dict[str, JsonValue] = Field(default={}, alias="with")
    #: Items in flight at once.
    concurrency: int = Field(default=4, ge=1, le=32)
    #: Hard cap on the list length; a longer list fails the agent before any item starts.
    max_items: int = Field(default=100, ge=1, le=1000)
    #: ``fail``: the first failed item fails the agent. ``collect``: a failed item yields ``null``
    #: and the agent emits ``partial`` (when routed) instead of ``default``.
    on_error: Literal["fail", "collect"] = "fail"


class MapAgent(_AgentBase):
    engine: Literal["map"]
    map: MapParams
    #: The strongest effect the child workflow has; never weaker (V015).
    effect: Effect = "write"


# parallel (unlike read-only agents at the same time)


BranchAgent = Annotated[CodeAgent | ToolAgent | LlmAgent, Field(discriminator="engine")]


class ParallelParams(_Strict):
    #: The agents run at the same time. Each reads its ``inputs`` from this agent's inputs, and the
    #: outputs of all of them together are this agent's outputs.
    branches: list[BranchAgent] = Field(min_length=2, max_length=16)
    #: Branches in flight at once; default: all of them.
    concurrency: int | None = Field(default=None, ge=1, le=16)
    #: ``fail``: the first failed branch fails the agent and stops the others. ``collect``: every
    #: branch runs, a failed one leaves its outputs ``null``, and the agent emits ``partial`` (when
    #: routed) instead of ``default``.
    on_error: Literal["fail", "collect"] = "fail"

    @model_validator(mode="before")
    @classmethod
    def _branches_do_not_route(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("branches"), list):
            fixed = []
            for b in data["branches"]:
                if isinstance(b, dict):
                    b = {"description": b.get("id", "branch"), "routes": {}, **b}
                fixed.append(b)
            return {**data, "branches": fixed}
        return data

    @model_validator(mode="after")
    def _unique_ids(self) -> ParallelParams:
        ids = [b.id for b in self.branches]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"branch ids must be unique: {', '.join(dupes)}")
        return self


class ParallelAgent(_AgentBase):
    engine: Literal["parallel"]
    parallel: ParallelParams


Agent = Annotated[
    PendingAgent
    | CodeAgent
    | ToolAgent
    | DecideAgent
    | LlmAgent
    | LoopAgent
    | HumanAgent
    | MapAgent
    | ParallelAgent,
    Field(discriminator="engine"),
]

ENGINES: tuple[str, ...] = (
    "pending",
    "code",
    "tool",
    "decide",
    "llm",
    "loop",
    "human",
    "map",
    "parallel",
)


# ── Workflow document (§7) ───────────────────────────────────────────────────


_MEDIA_RANGE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/([a-z0-9][a-z0-9!#$&^_.+-]*|\*)$")


class Upload(_Strict):
    """``multipart/form-data`` on a trigger: each ``file`` input is stored here."""

    store: str = Field(min_length=1, description="The `kind: Storage` store the files land in")
    prefix: str = Field(default="", description="Key prefix inside the store, e.g. `inbox/`")
    max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, description="Largest single file")
    max_files: int = Field(default=10, ge=1, le=100, description="Files per request")
    accept: list[str] = Field(
        default=[], description="Allowed content types (`application/pdf`, `image/*`); empty: any"
    )

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        from tuvl.storage.errors import InvalidKey
        from tuvl.storage.keys import validate_prefix

        try:
            return validate_prefix(value)
        except InvalidKey as exc:
            raise ValueError(f"upload prefix: {exc}") from exc

    @field_validator("accept")
    @classmethod
    def _accept(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _MEDIA_RANGE.fullmatch(item):
                raise ValueError(f"accept entry {item!r} must be a content type or type/*")
        return value


class HttpTrigger(_Strict):
    path: str = Field(pattern=r"^/[A-Za-z0-9_\-./{}]*$")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    public: bool = False
    sync_timeout: Duration = "30s"
    #: ``false`` keeps the route's settings but does not mount it.
    enabled: bool = True
    upload: Upload | None = None

    @model_validator(mode="after")
    def _upload_needs_a_body(self) -> HttpTrigger:
        if self.upload is not None and self.method in ("GET", "DELETE"):
            raise ValueError(f"upload needs a request body; {self.method} has none")
        return self


class McpExport(_Strict):
    enabled: bool = False


#: The pattern ``tuvl.tenancy.TENANT_ID_PATTERN`` enforces (kept in step by a test).
_TENANT_ID = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


class Schedule(_Strict):
    cron: str
    enabled: bool = True
    #: The trigger input every scheduled run starts with; checked against ``trigger.input``.
    input: dict[str, JsonValue] = {}

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        from cronsim import CronSim, CronSimError

        if len(value.split()) != 5:
            raise ValueError("schedule must be a 5-field cron expression (UTC)")
        try:
            CronSim(value, _dt.datetime(2026, 1, 1))
        except CronSimError as exc:
            raise ValueError(f"invalid cron expression {value!r}: {exc}") from exc
        return value


def _schedule_or_cron(schema: dict[str, Any]) -> None:
    schema["anyOf"].insert(
        1, {"type": "string", "description": "A cron expression: shorthand for {cron: …}"}
    )


class Trigger(_Strict):
    http: HttpTrigger | None = None
    schedule: Schedule | None = Field(default=None, json_schema_extra=_schedule_or_cron)
    #: Multi-tenant mode: the tenants a scheduled workflow runs for, one run per tenant per fire.
    tenants: list[str] = []
    mcp: McpExport | None = None
    input: FieldMap = {}

    _fields_ok = field_validator("input")(_check_field_map)

    @property
    def active_http(self) -> HttpTrigger | None:
        return self.http if self.http is not None and self.http.enabled else None

    @property
    def active_schedule(self) -> Schedule | None:
        return self.schedule if self.schedule is not None and self.schedule.enabled else None

    @field_validator("schedule", mode="before")
    @classmethod
    def _cron_shorthand(cls, value: Any) -> Any:
        return {"cron": value} if isinstance(value, str) else value

    @field_validator("tenants")
    @classmethod
    def _tenant_ids(cls, value: list[str]) -> list[str]:
        for tenant in value:
            if not _TENANT_ID.fullmatch(tenant):
                raise ValueError(f"tenant id {tenant!r} must match {_TENANT_ID.pattern}")
        if len(set(value)) != len(value):
            raise ValueError("tenants must not repeat")
        return value

    @model_validator(mode="after")
    def _tenants_need_a_schedule(self) -> Trigger:
        if self.tenants and not self.schedule:
            raise ValueError("'tenants' only applies to a scheduled trigger — add 'schedule'")
        return self


class EndOutput(_Strict):
    status: int = Field(default=200, ge=100, le=599)
    body: JsonValue = None
    type: FieldMap | None = None
    rollback: bool = False

    _fields_ok = field_validator("type")(lambda v: v if v is None else _check_field_map(v))


class Policy(_Strict):
    max_tokens_per_run: int | None = Field(default=None, ge=1)
    max_bounded_agents: int | None = Field(default=None, ge=0)
    require_human_before: list[Literal["write", "destructive"]] = []
    model_only_decisions: Literal["warn", "error", "allow"] = "warn"
    max_depth: int = Field(default=2, ge=1, le=5)
    allow_secure_to_llm: bool = False


class WorkflowMetadata(_Strict):
    name: WorkflowName
    description: str | None = None
    required_scope: str | None = None
    required_group: str | None = None
    #: how Insight groups workflows in its picker; purely organisational
    group: str | None = Field(default=None, max_length=64)
    tags: list[str] = []


def _default_outputs() -> dict[str, EndOutput]:
    return {"default": EndOutput()}


_SECRET_REF = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_ENV_URL = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(:[^}]*)?\}$")
WebhookEvent = Literal["completed", "failed", "aborted", "waiting"]


class Webhook(_Strict):
    """A notification to one HTTPS endpoint when a run reaches a stage (`spec.webhooks`).

    It carries only a run id and what happened; the receiver pulls the run with its own token.
    The URL and the secret come from the environment, never from a caller.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    url: str = Field(description="`${ENV_VAR}` (or an https:// literal); checked again when sent")
    secret: str = Field(description="`${ENV_VAR}` holding at least 32 bytes; signs every call")
    previous_secret: str = Field(
        default="",
        description="`${ENV_VAR}` of the secret being replaced; both sign during rotation",
    )
    events: list[WebhookEvent] = []
    stages: list[AgentId] = Field(
        default=[], description="Agent ids: notify each time one of them finishes"
    )
    allow_private: bool = Field(
        default=False, description="Permit an internal or http:// target (inside your network)"
    )

    @field_validator("secret")
    @classmethod
    def _secret(cls, value: str) -> str:
        if not _SECRET_REF.fullmatch(value):
            raise ValueError("secret must be a ${ENV_VAR} reference; never write it in the YAML")
        return value

    @field_validator("previous_secret")
    @classmethod
    def _previous(cls, value: str) -> str:
        if value and not _SECRET_REF.fullmatch(value):
            raise ValueError("previous_secret must be a ${ENV_VAR} reference")
        return value

    @model_validator(mode="after")
    def _checked(self) -> Webhook:
        from tuvl.webhooks.guard import url_problem

        if not _ENV_URL.fullmatch(self.url):
            problem = url_problem(self.url, allow_private=self.allow_private)
            if problem:
                raise ValueError(f"url {problem}")
        if not self.events and not self.stages:
            raise ValueError("subscribes to nothing: set 'events' and/or 'stages'")
        if len(set(self.events)) != len(self.events) or len(set(self.stages)) != len(self.stages):
            raise ValueError("'events' and 'stages' must not repeat")
        return self


class WorkflowSpec(_Strict):
    models: list[ModelPin] = []
    trigger: Trigger = Trigger()
    start: AgentId | None = None
    transaction: Literal["per_agent", "per_run"] = "per_agent"
    agents: list[Agent] = Field(min_length=1)
    outputs: dict[EndName, EndOutput] = Field(default_factory=_default_outputs)
    policy: Policy = Policy()
    supervisor: Supervisor | None = None
    hooks: list[ArtifactRef] = []
    webhooks: list[Webhook] = []

    def all_agents(self) -> list[Any]:
        """The agents, and the branches of each ``parallel`` step after it."""
        found: list[Any] = []
        for agent in self.agents:
            found.append(agent)
            if isinstance(agent, ParallelAgent):
                found.extend(agent.parallel.branches)
        return found

    @model_validator(mode="after")
    def _webhook_names(self) -> WorkflowSpec:
        names = [w.name for w in self.webhooks]
        if len(set(names)) != len(names):
            raise ValueError("webhook names must be unique")
        return self

    @property
    def entry(self) -> str:
        return self.start or self.agents[0].id


class WorkflowDocument(_Strict):
    kind: Literal["Workflow"]
    version: Literal["tuvl/v2"]
    enabled: bool = True
    metadata: WorkflowMetadata
    spec: WorkflowSpec


# ── Structured artifacts added in 2.0 (§5.3, §12) ───────────────────────────


class CalibrationExample(_Strict):
    input: str = Field(min_length=1, description="Path to a JSON fixture.")
    expect: Literal["pass", "fail", "uncertain"]


class JudgeSpec(_Strict):
    type: Literal["judge"]
    model: str = Field(min_length=1)
    rubric: str = Field(min_length=1)
    verdict: list[Literal["pass", "fail", "uncertain"]] = ["pass", "fail", "uncertain"]
    min_score: float = Field(default=0.5, ge=0.0, le=1.0)
    calibration: list[CalibrationExample] = []
    min_agreement: float = Field(default=0.9, ge=0.0, le=1.0)

    @field_validator("verdict")
    @classmethod
    def _verdicts(cls, value: list[str]) -> list[str]:
        if "pass" not in value or "fail" not in value or len(set(value)) != len(value):
            raise ValueError("verdict must contain 'pass' and 'fail', without duplicates")
        return value


class RulesSpec(_Strict):
    """A reusable decision table for ``decide.rules: artifact://…``."""

    type: Literal["rules"]
    rules: list[DecideRule] = Field(min_length=1)


#: Checkpoints a laya-serve accepts as ``model``; ``auto`` lets it route by language.
LAYA_CHECKPOINTS = ("auto", "english", "multilingual", "typed-decisions")


class DecisionModelSpec(_Strict):
    """``kind: AgentModel`` with ``spec.type: decision`` (§10.5).

    ``laya`` and ``jev`` are System One servers reached at ``api_base``; for ``laya``,
    ``model`` names the checkpoint (the weights are pinned by the server image).
    """

    type: Literal["decision"]
    provider: Literal["laya", "jev", "llm", "litellm"]
    model: str = Field(min_length=1)
    api_base: str | None = Field(
        default=None, min_length=1, description="Endpoint of a hosted decision model."
    )
    api_key: str | None = Field(
        default=None, description="An ${ENV_VAR} reference; a literal key is refused."
    )

    @model_validator(mode="after")
    def _systemone(self) -> DecisionModelSpec:
        if self.provider in ("laya", "jev") and not self.api_base:
            raise ValueError(f"provider '{self.provider}' needs api_base (the System One server)")
        if self.provider == "laya" and self.model not in LAYA_CHECKPOINTS:
            raise ValueError(f"laya model must be one of {', '.join(LAYA_CHECKPOINTS)}")
        return self

    @field_validator("api_key")
    @classmethod
    def _key_from_env(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*(:[^}]*)?\}", value):
            raise ValueError("api_key must be an ${ENV_VAR} reference — secrets live in .env")
        return value


# ── Test and TaskPlan (§18.1, §17.4) ─────────────────────────────────────────


class TestMock(_Strict):
    outputs: dict[str, JsonValue] | None = None
    signal: Signal | None = None

    @model_validator(mode="after")
    def _something(self) -> TestMock:
        if self.outputs is None and self.signal is None:
            raise ValueError("a mock needs 'outputs', 'signal', or both")
        return self


class JudgeExpectation(_Strict):
    judge: ArtifactRef
    target: str = Field(min_length=1, description="Template selecting the judged value.")


class DecisionExpectation(_Strict):
    route: Signal | None = None
    source: str | None = Field(default=None, pattern=r"^(rule#\d+|model|shadow)$")


class TestExpect(_Strict):
    end: EndName | None = None
    path: list[AgentId] | None = None
    path_includes: list[AgentId] = []
    wait: Literal["human", "approval"] | None = Field(
        default=None,
        description="The run must stop for a person: 'human' (a human agent) or 'approval' "
        "(a human agent or an in-loop tool approval). Unmocked human agents suspend.",
    )
    decisions: dict[AgentId, DecisionExpectation] = {}
    output: JsonValue = None
    assertions: list[str] = []
    judge: list[JudgeExpectation] = []

    @field_validator("assertions")
    @classmethod
    def _tel(cls, value: list[str]) -> list[str]:
        for text in value:
            _check_tel(text)
        return value

    @model_validator(mode="after")
    def _end_or_wait(self) -> TestExpect:
        if self.wait is not None and self.end is not None:
            raise ValueError("expect either 'end' or 'wait', not both")
        return self


class TestCaller(_Strict):
    """Who starts the test run: becomes the run's principal (``ctx.principal``)."""

    user: str = Field(min_length=1)


class TestSpec(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    workflow: WorkflowName
    #: Rows that exist before the run, per model, in the test's own database.
    given: dict[ModelName, list[dict[str, JsonValue]]] = {}
    caller: TestCaller | None = Field(default=None, alias="as")
    input: dict[str, JsonValue] = {}
    mocks: dict[AgentId, TestMock] = {}
    replay: str | None = None
    expect: TestExpect = TestExpect()


class TestMetadata(_Strict):
    name: str = Field(min_length=1)
    generated_from: str | None = None


class TestDocument(_Strict):
    __test__ = False  # not a pytest class

    kind: Literal["Test"]
    version: Literal["tuvl/v2"]
    metadata: TestMetadata
    spec: TestSpec


class SpecRef(_Strict):
    anchor: str = Field(min_length=1)
    hash: str = Field(min_length=1)


class Task(_Strict):
    # `T3` (a list-local id) or `TASK-<INITIALS>-<idea>-<spec>-<task>`; tuvl_insight.spec.ids holds the same pattern
    id: str = Field(pattern=r"^(T\d+|TASK-[A-Z][A-Z0-9]{1,3}-\d{4,}-\d{2,}-\d{3,})$")
    title: str = Field(min_length=1)
    type: Literal[
        "model", "workflow", "agent", "artifact", "integration", "test", "policy", "manual"
    ]
    spec_ref: SpecRef | None = None
    target: dict[str, str] = {}
    depends_on: list[str] = []
    status: Literal["derived", "todo", "in_progress", "done"] = "derived"
    obsolete: bool = Field(
        default=False, description="Its step or example left the spec; kept for the record."
    )

    @model_validator(mode="after")
    def _manual_status(self) -> Task:
        if self.type != "manual" and self.status != "derived":
            raise ValueError("only manual tasks take a manual status; others are derived")
        return self


class TaskPlanMetadata(_Strict):
    name: str = Field(min_length=1)
    spec: str = Field(min_length=1)
    spec_hash: str = Field(min_length=1)


class TaskPlanSpec(_Strict):
    tasks: list[Task] = []


class TaskPlanDocument(_Strict):
    kind: Literal["TaskPlan"]
    version: Literal["tuvl/v2"]
    metadata: TaskPlanMetadata
    spec: TaskPlanSpec


# ── JSON Schema export ───────────────────────────────────────────────────────

DOCUMENT_MODELS: dict[str, type[BaseModel]] = {
    "Workflow": WorkflowDocument,
    "Test": TestDocument,
    "TaskPlan": TaskPlanDocument,
}


def workflow_json_schema() -> dict[str, Any]:
    """JSON Schema for a 2.0 ``Workflow`` document (Insight forms, editors, docs)."""
    schema = WorkflowDocument.model_json_schema()
    schema["$id"] = "https://tuvl.dev/schema/2.0/workflow.json"
    return schema


def document_json_schemas() -> dict[str, dict[str, Any]]:
    """JSON Schema for every 2.0 document kind, keyed by ``kind``."""
    return {kind: model.model_json_schema() for kind, model in DOCUMENT_MODELS.items()}
