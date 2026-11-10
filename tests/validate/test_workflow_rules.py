"""2.0 validator rules (spec Appendix B) — each test breaks the spec's own
Appendix A example in one way and checks the rule that catches it."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from tuvl.contract.schema import WorkflowDocument
from tuvl_insight.validate import Catalog, WorkflowReport, validate_workflow

FIXTURE = Path(__file__).parents[1] / "contract" / "fixtures" / "refund_flow.yaml"
BASE: dict[str, Any] = yaml.safe_load(FIXTURE.read_text())


def catalog() -> Catalog:
    cat = Catalog()
    models = {
        "Order": [
            {"name": "id", "type": "uuid", "primary_key": True},
            {"name": "total", "type": "numeric", "required": True},
            {"name": "owner_id", "type": "uuid"},
            {"name": "status", "type": "enum", "enum_values": ["open", "paid"], "required": True},
        ],
        "Refund": [
            {"name": "id", "type": "uuid", "primary_key": True},
            {"name": "order_id", "type": "uuid", "required": True},
            {"name": "amount", "type": "numeric", "required": True},
            {"name": "status", "type": "string"},
        ],
    }
    for name, fields in models.items():
        cat.add_model(
            {"kind": "ModelDefinition", "metadata": {"name": name}, "spec": {"fields": fields}}
        )
    cat.artifacts = {
        "support-voice": ("prompt", {3}),
        "refund-reply-quality": ("judge", {2}),
        "audit-log": ("hook", {1}),
        "zendesk": ("mcp", {3}),
        "refund-rules": ("rules", {4}),
    }
    cat.agent_models = {"triage-classifier": "decision", "default": "llm", "gpt": "llm"}
    return cat


def run(
    mutate: Callable[[dict[str, Any]], None] | None = None, cat: Catalog | None = None
) -> WorkflowReport:
    doc = copy.deepcopy(BASE)
    if mutate:
        mutate(doc)
    return validate_workflow(
        WorkflowDocument.model_validate(doc),
        cat or catalog(),
        text=yaml.safe_dump(doc, sort_keys=False),
    )


def agent(doc: dict[str, Any], agent_id: str) -> dict[str, Any]:
    return next(a for a in doc["spec"]["agents"] if a["id"] == agent_id)


def found(report: WorkflowReport, code: str, fragment: str, severity: str = "error") -> bool:
    return any(
        d.code == code and d.severity == severity and fragment in str(d) for d in report.diagnostics
    )


def test_appendix_a_is_clean_and_profiled() -> None:
    report = run()
    assert report.diagnostics == []
    assert report.profile.summary() == (
        "6 agents (1 off-spine) · 3 deterministic · 2 bounded · 1 external · worst-case 1.5k tok + judges"
    )


# ── V002 / V003 ─────────────────────────────────────────────────────────────


def test_v002_duplicate_id() -> None:
    def m(d: dict) -> None:
        d["spec"]["agents"].append({**copy.deepcopy(agent(d, "reply"))})

    assert found(run(m), "V002", "duplicate agent id 'reply'")


@pytest.mark.parametrize(
    ("target", "fragment"),
    [
        ("nowhere", "route target 'nowhere' is not an agent"),
        (
            "END.cancelled",
            "'END.cancelled' is not a declared output — outputs: default, failed, not_found, rejected",
        ),
        ("void_refund", "'void_refund' is off-spine"),
    ],
)
def test_v003_route_targets(target: str, fragment: str) -> None:
    assert found(
        run(lambda d: agent(d, "reply")["routes"].update(default=target)), "V003", fragment
    )


def test_v003_start_and_compensate() -> None:
    assert found(
        run(lambda d: d["spec"].update(start="ghost")), "V003", "start agent 'ghost' is not defined"
    )
    assert found(
        run(lambda d: d["spec"].update(start="void_refund")),
        "V003",
        "entry agent 'void_refund' is off-spine",
    )
    assert found(
        run(lambda d: agent(d, "issue_refund").update(compensate="ghost")),
        "V003",
        "compensator 'ghost' is not defined",
    )


# ── V004 ────────────────────────────────────────────────────────────────────


def test_v004_unrouted_reserved_signal() -> None:
    report = run(lambda d: agent(d, "load_order")["routes"].pop("not_found"))
    assert found(report, "V004", "'load_order' can emit 'not_found' but it is not routed")
    report = run(lambda d: agent(d, "reply")["routes"].pop("timeout"))
    assert found(report, "V004", "'reply' can emit 'timeout'")


def test_v004_unrouted_error_is_a_warning() -> None:
    report = run(lambda d: agent(d, "issue_refund")["routes"].pop("error"))
    assert found(
        report, "V004", "'error' is not routed — an error ends the run as failed", "warning"
    )
    assert not report.errors


def test_v004_signal_never_emitted() -> None:
    assert found(
        run(lambda d: agent(d, "load_order")["routes"].update(conflict="END.failed")),
        "V004",
        "'load_order' never emits 'conflict'",
    )
    assert found(
        run(lambda d: agent(d, "approval")["routes"].update(maybe="END")),
        "V004",
        "'approval' never emits 'maybe'",
    )


def test_v004_human_bool_routes_must_cover_both_values() -> None:
    assert found(
        run(lambda d: agent(d, "approval")["routes"].pop("false")),
        "V004",
        "'approval' can emit 'false'",
    )


def test_v004_guardrails_add_guardrail_violation() -> None:
    assert found(
        run(lambda d: agent(d, "reply")["routes"].pop("guardrail_violation")),
        "V004",
        "can emit 'guardrail_violation'",
    )


# ── V005 ────────────────────────────────────────────────────────────────────


def test_v005_unreachable_agent() -> None:
    def m(d: dict) -> None:
        d["spec"]["agents"].append(
            {
                "id": "orphan",
                "description": "never used",
                "engine": "code",
                "code": {"run": "x"},
                "routes": {"default": "END", "error": "END.failed"},
            }
        )

    assert found(
        run(m), "V005", "'orphan' is unreachable from the entry agent 'load_order'", "warning"
    )


def test_v005_unguarded_cycle() -> None:
    def m(d: dict) -> None:
        agent(d, "issue_refund")["routes"]["default"] = "load_order"

    # load_order → triage → issue_refund → load_order repeats without the human
    # (the approval branch is guarded, the auto_refund branch is not).
    assert found(
        run(m), "V005", "cycle load_order, triage_refund, issue_refund can repeat", "warning"
    )


def test_v005_cycle_through_human_is_fine() -> None:
    report = run(lambda d: agent(d, "approval")["routes"].update(expired="load_order"))
    assert not any(d.code == "V005" for d in report.diagnostics)


# ── V006 / V007 / V009 ──────────────────────────────────────────────────────


def test_v006_input_not_produced_on_every_path() -> None:
    def m(d: dict) -> None:
        agent(d, "triage_refund")["routes"]["auto_refund"] = (
            "reply"  # skips issue_refund → no refund
        )

    report = run(m)
    assert found(
        report,
        "V006",
        "input 'refund' is not produced on every path to 'reply' — missing when reached from 'triage_refund' via 'auto_refund'",
    )


def test_v006_input_not_a_trigger_input() -> None:
    assert found(
        run(lambda d: agent(d, "load_order")["inputs"].update(customer_id="uuid")),
        "V006",
        "it is not a trigger input",
    )


def test_v006_optional_inputs_need_not_be_produced() -> None:
    assert not run(lambda d: agent(d, "reply")["inputs"].update(coupon="str?")).errors


def test_v006_reserved_signal_does_not_carry_outputs() -> None:
    # 'low_confidence' leaves triage without its decision, so 'route' is not available.
    report = run(
        lambda d: agent(d, "approval")["inputs"].update(
            route="enum[auto_refund, manager_review, reject]"
        )
    )
    assert found(report, "V006", "input 'route' is not produced on every path to 'approval'")


def test_v007_type_mismatch_between_producer_and_consumer() -> None:
    report = run(lambda d: agent(d, "reply")["inputs"].update(refund="Order"))
    assert found(
        report, "V007", "input 'refund' (Order) from 'issue_refund' (Refund): Refund is not Order"
    )


def test_v007_optional_producer_into_required_consumer() -> None:
    report = run(lambda d: d["spec"]["trigger"]["input"].update(message="str?"))
    assert found(report, "V007", "input 'message' (str) from the trigger input (str?): may be null")


def test_v007_int_widens_to_float_and_enum_narrows() -> None:
    def m(d: dict) -> None:
        d["spec"]["trigger"]["input"]["reason"] = "enum[damaged, fraud_flag]"
        agent(d, "triage_refund")["inputs"]["reason"] = "enum[damaged, fraud_flag, other]"

    assert not run(m).errors

    def bad(d: dict) -> None:
        d["spec"]["trigger"]["input"]["reason"] = "enum[damaged, lost]"
        agent(d, "triage_refund")["inputs"]["reason"] = "enum[damaged, fraud_flag]"

    assert found(run(bad), "V007", "has values ['lost']")


def test_v009_rewrite_of_an_existing_key() -> None:
    report = run(lambda d: agent(d, "issue_refund")["outputs"].update(order="Order"))
    assert found(report, "V009", "'order' is also written by 'load_order'", "warning")
    report = run(lambda d: agent(d, "issue_refund")["outputs"].update(order="Refund"))
    assert found(report, "V007", "'order' is re-written as Refund but 'load_order' wrote Order")


def test_v006_compensator_inputs_come_from_the_compensated_agent() -> None:
    assert found(
        run(lambda d: agent(d, "void_refund")["inputs"].update(ticket="str")),
        "V006",
        "compensator input 'ticket' is neither an input nor an output of 'issue_refund'",
    )


# ── V008 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (
            lambda d: agent(d, "triage_refund")["decide"]["rules"][0].update(
                when="order.totl > 500"
            ),
            "Order has no field 'totl'",
        ),
        (
            lambda d: agent(d, "triage_refund")["decide"]["rules"][0].update(
                when="order.total + 1"
            ),
            "condition must be a bool",
        ),
        (
            lambda d: agent(d, "triage_refund")["decide"]["rules"][1].update(
                when='refund_reason == "x"'
            ),
            "unknown name 'refund_reason'",
        ),
        (
            lambda d: agent(d, "triage_refund")["decide"]["rules"][0].update(
                when='order.status == "shipped"'
            ),
            "'shipped' is not a value of",
        ),
        (
            lambda d: agent(d, "load_order")["tool"]["where"].update(id="{{ order_ref }}"),
            "unknown name 'order_ref'",
        ),
        (
            lambda d: agent(d, "approval")["human"].update(title="Refund {{ order.missing }}"),
            "Order has no field 'missing'",
        ),
        (
            lambda d: agent(d, "approval")["human"]["show"].append("refund"),
            "'refund' is not a declared input",
        ),
        (
            lambda d: agent(d, "reply")["llm"].update(prompt="Confirm {{ refund.amount + }}"),
            "expected a value",
        ),
    ],
)
def test_v008_expressions_and_templates(mutate: Callable[[dict], None], fragment: str) -> None:
    assert found(run(mutate), "V008", fragment)


# ── V010 / V011 / V012 ──────────────────────────────────────────────────────


def test_v010_pending_is_a_warning() -> None:
    def m(d: dict) -> None:
        a = agent(d, "issue_refund")
        a["engine"] = "pending"
        del a["code"]
        a.pop("effect")

    report = run(m)
    assert found(report, "V010", "'issue_refund' is pending", "warning")
    assert report.profile.pending == 1


def test_v011_budgets() -> None:
    assert found(
        run(lambda d: agent(d, "reply")["budget"].pop("max_tokens")),
        "V011",
        "llm agents must set budget.max_tokens",
    )
    assert found(
        run(lambda d: agent(d, "triage_refund")["budget"].pop("timeout")),
        "V011",
        "decide agents must set budget.timeout",
    )
    assert found(
        run(lambda d: agent(d, "reply")["budget"].update(max_iterations=3)),
        "V011",
        "only applies to loop agents",
        "warning",
    )


def test_v012_decide_enum_and_routes() -> None:
    assert found(
        run(lambda d: agent(d, "triage_refund")["routes"].pop("reject")),
        "V012",
        "decision 'reject' is not routed",
    )
    assert found(
        run(lambda d: agent(d, "triage_refund")["routes"].update(escalate="approval")),
        "V012",
        "'escalate' is not a value of the decision enum",
    )
    assert found(
        run(lambda d: agent(d, "triage_refund")["decide"]["rules"][0].update(then="refund")),
        "V012",
        "'refund' is not a value",
    )
    assert found(
        run(lambda d: agent(d, "triage_refund")["outputs"].update(extra="str")),
        "V012",
        "exactly one output",
    )


def test_v012_low_confidence_needs_a_deciding_model() -> None:
    def m(d: dict) -> None:
        agent(d, "triage_refund")["decide"]["shadow"] = True

    assert found(run(m), "V012", "'low_confidence' is never emitted — shadow mode")


def test_v011_llm_backed_decide_needs_max_tokens() -> None:
    report = run(lambda d: agent(d, "triage_refund")["decide"]["model"].update(ref="gpt"))
    assert found(report, "V011", "decide agents must set budget.max_tokens")


# ── V014 / V016 / V017 / V018 / V019 / V022 ─────────────────────────────────


def _with_loop(d: dict, tools: list[dict]) -> None:
    d["spec"]["agents"].append(
        {
            "id": "resolver",
            "description": "resolve",
            "engine": "loop",
            "inputs": {"order": "Order"},
            "outputs": {"note": "str"},
            "budget": {
                "max_iterations": 4,
                "max_tokens": 2000,
                "max_tool_calls": 5,
                "timeout": "1m",
            },
            "loop": {"model": "default", "steering": "Resolve it.", "tools": tools},
            "routes": {
                "resolved": "END.rejected",
                "max_iterations": "END.failed",
                "budget_exceeded": "END.failed",
                "timeout": "END.failed",
                "aborted": "END.failed",
                "error": "END.failed",
            },
        }
    )
    agent(d, "triage_refund")["routes"]["reject"] = "resolver"


def test_v014_and_approval_conditions_over_tool_args() -> None:
    report = run(lambda d: _with_loop(d, [{"agent": "ghost"}]))
    assert found(report, "V014", "tool agent 'ghost' is not defined")
    ok = run(
        lambda d: _with_loop(
            d, [{"agent": "void_refund", "approval": {"when": "args.refund.amount > 100"}}]
        )
    )
    assert not ok.errors, [str(e) for e in ok.errors]
    bad = run(
        lambda d: _with_loop(
            d, [{"agent": "void_refund", "approval": {"when": "args.amount > 100"}}]
        )
    )
    assert found(bad, "V008", "has no field 'amount'")


def test_loop_tokens_are_counted() -> None:
    report = run(lambda d: _with_loop(d, [{"agent": "void_refund"}]))
    assert report.profile.worst_case_tokens == 2000  # max(reply 1.5k, resolver 2k)
    assert report.profile.bounded == 3


def test_token_spending_cycle_is_unbounded() -> None:
    report = run(lambda d: agent(d, "reply")["routes"].update(guardrail_violation="reply"))
    assert report.profile.worst_case_tokens is None
    assert "worst-case unbounded" in report.profile.summary()


def test_v016_human_form() -> None:
    assert found(
        run(lambda d: agent(d, "approval")["human"].update(route_on="note")),
        "V016",
        "non-optional bool or enum",
    )
    assert found(
        run(lambda d: agent(d, "approval")["human"].update(route_on="ghost")),
        "V016",
        "route_on 'ghost' is not an output",
    )
    assert found(
        run(lambda d: agent(d, "approval")["human"].update(form={"ghost": {"label": "x"}})),
        "V016",
        "form field 'ghost'",
    )


def test_v017_artifacts() -> None:
    assert found(
        run(lambda d: agent(d, "reply")["llm"].update(system="artifact://nope@1")),
        "V017",
        "artifact 'nope' is not defined",
    )
    assert found(
        run(lambda d: agent(d, "reply")["llm"].update(system="artifact://refund-reply-quality@2")),
        "V017",
        "is a judge artifact; this field takes prompt",
    )
    assert found(
        run(lambda d: agent(d, "reply")["llm"].update(system="artifact://support-voice@9")),
        "V017",
        "has no version 9",
    )
    assert found(
        run(lambda d: agent(d, "reply")["llm"].update(system="artifact://support-voice")),
        "V017",
        "is not pinned",
        "warning",
    )
    assert found(
        run(lambda d: d["spec"].update(hooks=["artifact://support-voice@3"])),
        "V017",
        "this field takes hook",
    )


def test_v018_models() -> None:
    assert found(
        run(lambda d: d["spec"].update(models=["Order"])), "V018", "'Refund' is not in spec.models"
    )
    assert found(
        run(lambda d: d["spec"]["models"].append("Ghost")), "V018", "model 'Ghost' is not defined"
    )
    assert found(
        run(lambda d: agent(d, "reply")["inputs"].update(customer="Customer?")),
        "V018",
        "unknown model 'Customer'",
    )


def test_v019_trigger() -> None:
    def public(d: dict) -> None:
        d["spec"]["trigger"]["http"]["public"] = True

    assert found(run(public), "V019", "public: true cannot be combined")
    assert found(
        run(lambda d: d["metadata"].pop("description")),
        "V019",
        "MCP export needs metadata.description",
    )


def test_v022_end_bodies() -> None:
    def m(d: dict) -> None:
        d["spec"]["outputs"]["rejected"]["body"] = {"order": "{{ refund.id }}"}

    assert found(run(m), "V022", "'refund' is not produced on every path to END.rejected")
    report = run(
        lambda d: d["spec"]["outputs"]["default"]["body"].update(extra="{{ refund.missing }}")
    )
    assert found(report, "V022", "Refund has no field 'missing'")


# ── Locations ───────────────────────────────────────────────────────────────


def test_diagnostics_carry_author_paths_and_lines() -> None:
    report = run(
        lambda d: agent(d, "triage_refund")["decide"]["rules"][0].update(when="order.totl > 500")
    )
    (d,) = [d for d in report.diagnostics if d.code == "V008"]
    assert d.path == "spec.agents[triage_refund].decide.rules[0].when"
    assert d.line is not None and d.line > 1
    assert d.to_json()["code"] == "V008"


# ── Per-signal outputs (Q10) ────────────────────────────────────────────────


def _verifier(d: dict, outputs: dict) -> None:
    """Insert a code verifier between issue_refund's trigger and persist-like use."""
    a = agent(d, "issue_refund")
    a["outputs"] = outputs
    a["routes"] = {
        "valid": "reply",
        "partial": "reply",
        "missing": "END.failed",
        "error": "END.failed",
    }


def test_per_signal_outputs_flow_only_on_their_signal() -> None:
    # 'refund' exists on 'valid' only; 'reply' is reachable via 'partial' too → V006.
    report = run(
        lambda d: _verifier(
            d, {"valid": {"refund": "Refund"}, "partial": {"reason": "str"}, "missing": {}}
        )
    )
    assert found(
        report,
        "V006",
        "input 'refund' is not produced on every path to 'reply' — missing when reached from 'issue_refund' via 'partial'",
    )


def test_per_signal_outputs_satisfy_downstream_when_every_route_produces() -> None:
    report = run(
        lambda d: _verifier(
            d, {"valid": {"refund": "Refund"}, "partial": {"refund": "Refund"}, "missing": {}}
        )
    )
    assert not report.errors, [str(e) for e in report.errors]


def test_per_signal_outputs_keep_exact_types() -> None:
    # The union view makes 'refund' optional, but producers are checked with
    # the exact per-signal type — no false "may be null".
    report = run(
        lambda d: _verifier(
            d,
            {
                "valid": {"refund": "Refund"},
                "partial": {"refund": "Refund"},
                "missing": {"reason": "str"},
            },
        )
    )
    assert not any(d.code == "V007" for d in report.diagnostics)


def test_per_signal_outputs_must_agree_on_types() -> None:
    doc = copy.deepcopy(BASE)
    _verifier(doc, {"valid": {"refund": "Refund"}, "partial": {"refund": "Order"}, "missing": {}})
    with pytest.raises(Exception, match="a field has one type on every signal"):
        WorkflowDocument.model_validate(doc)


def test_decide_rejects_per_signal_outputs() -> None:
    def m(d: dict) -> None:
        agent(d, "triage_refund")["outputs"] = {"auto_refund": {"route": "str"}}

    assert found(run(m), "V012", "per-signal outputs do not apply")


def test_v007_db_result_lands_in_one_matching_output() -> None:
    def two_outputs(doc: dict[str, Any]) -> None:
        agent(doc, "load_order")["outputs"] = {"order": "Order", "extra": "str"}

    def wrong_model(doc: dict[str, Any]) -> None:
        agent(doc, "load_order")["outputs"] = {"order": "Refund"}

    def delete_with_output(doc: dict[str, Any]) -> None:
        a = agent(doc, "void_refund")
        a["tool"] = {
            "use": "db",
            "model": "Refund",
            "op": "delete",
            "where": {"id": "{{ refund.id }}"},
        }
        a["outputs"] = {"gone": "Refund"}
        a["routes"]["not_found"] = "END"

    assert found(run(two_outputs), "V007", "writes its result to one output")
    assert found(run(wrong_model), "V007", "returns Order, not Refund")
    assert found(run(delete_with_output), "V007", "'delete' produces no output")


def test_v025_builtin_params_and_v007_search_output() -> None:
    def search(with_: dict[str, Any], outputs: dict[str, str]) -> Callable[[dict[str, Any]], None]:
        def mutate(doc: dict[str, Any]) -> None:
            doc["spec"]["agents"].append(
                {
                    "id": "lookup",
                    "description": "kb",
                    "engine": "code",
                    "effect": "read",
                    "code": {"run": "tuvl.data_search", "with": with_},
                    "inputs": {"message": "str"},
                    "outputs": outputs,
                    "routes": {"default": "END"},
                }
            )
            agent(doc, "load_order")["routes"]["default"] = "lookup"
            agent(doc, "lookup")["routes"]["default"] = "triage_refund"

        return mutate

    good = {"collection": "kb", "query": "{{ message }}"}
    assert not [
        d
        for d in run(search(good, {"hits": "list[json]"})).diagnostics
        if d.code in ("V025", "V007")
    ]
    assert found(
        run(search({"query": "{{ message }}"}, {"hits": "list[json]"})),
        "V025",
        "needs code.with.collection",
    )
    assert found(run(search({**good, "k": 3}, {"hits": "list[json]"})), "V025", "does not take 'k'")
    assert found(run(search(good, {"hits": "json"})), "V007", "one list output")
    assert found(
        run(search({**good, "query": "{{ nope }}"}, {"hits": "list[json]"})), "V008", "nope"
    )

    def unknown(doc: dict[str, Any]) -> None:
        agent(doc, "issue_refund")["code"]["run"] = "tuvl.teleport"

    assert found(run(unknown), "V025", "not a tuvl built-in")


def test_v015_effects() -> None:
    def drop_compensate(doc: dict[str, Any]) -> None:
        agent(doc, "issue_refund").pop("compensate")

    # code agents write inside their own step transaction — not V015's concern
    assert not [d for d in run(drop_compensate).diagnostics if d.code == "V015"]

    def http_write_before_more_steps(doc: dict[str, Any]) -> None:
        a = agent(doc, "load_order")
        a["tool"] = {"use": "http", "method": "POST", "url": "https://x.test/hold"}
        a["outputs"] = {}
        a["routes"].pop("not_found")

    assert found(
        run(http_write_before_more_steps),
        "V015",
        "'load_order' has a write effect",
        severity="warning",
    )

    def weaker(doc: dict[str, Any]) -> None:
        agent(doc, "void_refund")["effect"] = "read"

    assert found(run(weaker), "V015", "weaker than the derived 'write'", severity="warning")


def test_v015_http_post_may_declare_read() -> None:
    def lookup(effect: str | None) -> Callable[[dict[str, Any]], None]:
        def mutate(doc: dict[str, Any]) -> None:
            a = agent(doc, "load_order")
            a["tool"] = {"use": "http", "method": "POST", "url": "https://x.test/search"}
            a["outputs"] = {}
            a["routes"].pop("not_found")
            if effect:
                a["effect"] = effect

        return mutate

    assert found(run(lookup(None)), "V015", "has a write effect", severity="warning")
    assert not [d for d in run(lookup("read")).diagnostics if d.code == "V015"]


def test_v021_per_run_cannot_wait() -> None:
    def per_run_with_human(doc: dict[str, Any]) -> None:
        doc["spec"]["transaction"] = "per_run"

    assert found(run(per_run_with_human), "V021", "'approval' can wait for a person")

    def per_run_scheduled(doc: dict[str, Any]) -> None:
        doc["spec"]["transaction"] = "per_run"
        doc["spec"]["trigger"]["schedule"] = "0 * * * *"

    assert found(run(per_run_scheduled), "V021", "synchronous only")


def test_v013_model_only_decision_feeding_a_write() -> None:
    def model_only(policy: str | None = None) -> Callable[[dict[str, Any]], None]:
        def mutate(doc: dict[str, Any]) -> None:
            triage = agent(doc, "triage_refund")
            triage["decide"]["rules"] = []
            triage["routes"]["auto_refund"] = "issue_refund"
            agent(doc, "issue_refund").pop("compensate")
            if policy:
                doc["spec"].setdefault("policy", {})["model_only_decisions"] = policy

        return mutate

    assert found(run(model_only()), "V013", "feeds issue_refund", severity="warning")
    assert found(run(model_only("error")), "V013", "feeds issue_refund")
    assert not [d for d in run(model_only("allow")).diagnostics if d.code == "V013"]
    # with rules, or with a compensator on the write, there is nothing to flag
    assert not [d for d in run().diagnostics if d.code == "V013"]


def test_v026_decision_provider_installed_and_pinned() -> None:
    def cat(provider: str, model: str) -> Catalog:
        c = catalog()
        c.decision_models = {"triage-classifier": (provider, model)}
        return c

    assert found(run(cat=cat("acme", "acme-1.2")), "V026", "not installed")
    # a laya model names a checkpoint; the server image pins the weights
    assert not [d for d in run(cat=cat("laya", "auto")).diagnostics if d.code == "V026"]
    assert found(
        run(cat=cat("llm", "openai/gpt-4o-latest")), "V026", "not pinned", severity="warning"
    )
    assert not [
        d for d in run(cat=cat("llm", "openai/gpt-4o-2024-08-06")).diagnostics if d.code == "V026"
    ]


def test_v014_human_agents_are_not_loop_tools() -> None:
    def human_tool(doc: dict[str, Any]) -> None:
        doc["spec"]["agents"].append(
            {
                "id": "investigate",
                "description": "look into it",
                "engine": "loop",
                "inputs": {"order": "Order"},
                "outputs": {"note": "str"},
                "budget": {
                    "max_iterations": 3,
                    "max_tokens": 1000,
                    "max_tool_calls": 3,
                    "timeout": "30s",
                },
                "loop": {
                    "model": "default",
                    "steering": "Investigate.",
                    "tools": [{"agent": "approval"}],
                },
                "routes": {
                    "default": "END",
                    "error": "END.failed",
                    "max_iterations": "END.failed",
                    "budget_exceeded": "END.failed",
                    "timeout": "END.failed",
                    "aborted": "END.failed",
                    "parse_error": "END.failed",
                },
            }
        )

    assert found(run(human_tool), "V014", "'approval' is a human agent")


def test_v020_policy() -> None:
    def policy(**rules: Any) -> Callable[[dict[str, Any]], None]:
        def mutate(doc: dict[str, Any]) -> None:
            doc["spec"]["policy"] = {**doc["spec"].get("policy", {}), **rules}

        return mutate

    assert found(run(policy(max_tokens_per_run=100)), "V020", "exceeds max_tokens_per_run 100")
    assert found(run(policy(max_bounded_agents=0)), "V020", "exceed max_bounded_agents 0")
    # the refund is issued without a human on the auto_refund path
    assert found(
        run(policy(require_human_before=["write"])), "V020", "'issue_refund' has a write effect"
    )

    secure = catalog()
    secure.secure_fields = {"Order": {"owner_id"}}
    assert found(run(cat=secure), "V020", "carries secure fields ['owner_id'] of Order")
    assert not [
        d for d in run(policy(allow_secure_to_llm=True), cat=secure).diagnostics if d.code == "V020"
    ]


def test_v019_scheduled_input_must_match_the_trigger_input() -> None:
    def scheduled(value: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
        def mutate(doc: dict[str, Any]) -> None:
            doc["spec"]["trigger"]["schedule"] = {"cron": "0 6 * * *", "input": value}

        return mutate

    good = {
        "order_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "reason": "late",
        "message": "hi",
    }
    assert not [d for d in run(scheduled(good)).diagnostics if d.code == "V019"]
    assert found(run(scheduled({"reason": "late"})), "V019", "scheduled input")
    assert found(run(scheduled({**good, "order_id": "nope"})), "V019", "scheduled input")

    def disabled(doc: dict[str, Any]) -> None:
        doc["spec"]["trigger"]["schedule"] = {"cron": "0 6 * * *", "enabled": False, "input": {}}

    assert not [d for d in run(disabled).diagnostics if d.code == "V019"]


# ── V037: file uploads ──────────────────────────────────────────────────────


def _uploads(
    *,
    input_: dict[str, Any] | None = None,
    upload: dict[str, Any] | None = None,
    mcp: bool = False,
    public: bool = False,
) -> Callable[[dict[str, Any]], None]:
    def mutate(doc: dict[str, Any]) -> None:
        trigger = doc["spec"]["trigger"]
        trigger["input"] = {**trigger["input"], **(input_ or {})}
        if upload is not None:
            trigger["http"]["upload"] = upload
        trigger["http"]["public"] = public
        trigger["mcp"] = {"enabled": mcp}
        doc["metadata"].pop("required_scope", None)
        doc["metadata"].pop("required_group", None)

    return mutate


def _stores(**extra: dict[str, Any]) -> Catalog:
    from tuvl.storage.spec import StoreSpec

    cat = catalog()
    cat.stores = {
        "inbox": StoreSpec(provider="local"),
        **{k: StoreSpec(**v) for k, v in extra.items()},
    }
    return cat


def test_v037_upload_rules() -> None:
    ok = run(_uploads(input_={"doc": "file"}, upload={"store": "inbox"}), _stores())
    assert not [d for d in ok.diagnostics if d.code == "V037"]

    no_file = run(_uploads(upload={"store": "inbox"}), _stores())
    assert found(no_file, "V037", "no trigger input is a file")

    no_upload = run(_uploads(input_={"doc": "file"}), _stores())
    assert found(no_upload, "V037", "cannot receive doc", severity="warning")

    nested = run(_uploads(input_={"doc": "list[list[file]]"}, upload={"store": "inbox"}), _stores())
    assert found(nested, "V037", "not nested deeper")

    mcp = run(_uploads(input_={"doc": "file"}, upload={"store": "inbox"}, mcp=True), _stores())
    assert found(mcp, "V037", "MCP export cannot take file inputs")

    public = run(
        _uploads(input_={"doc": "file"}, upload={"store": "inbox"}, public=True), _stores()
    )
    assert found(public, "V037", "anyone write files", severity="warning")


def test_v037_the_store_must_exist_be_writable_and_big_enough() -> None:
    spec = {"input_": {"doc": "file"}}
    missing = run(_uploads(**spec, upload={"store": "nope"}), _stores())
    assert found(missing, "V037", "no Storage 'nope'")

    ro = run(
        _uploads(**spec, upload={"store": "ro"}),
        _stores(ro={"provider": "local", "readonly": True}),
    )
    assert found(ro, "V037", "read-only")

    small = run(
        _uploads(**spec, upload={"store": "tiny", "max_bytes": 5000}),
        _stores(tiny={"provider": "local", "max_object_bytes": 1000}),
    )
    assert found(small, "V037", "exceeds the store's max_object_bytes")


def test_a_loop_collection_tool_must_name_a_defined_collection() -> None:
    def with_collection(name: str) -> Callable[[dict], None]:
        return lambda d: _with_loop(d, [{"collection": name, "top_k": 3}])

    cat = catalog()
    cat.collections = {"kb"}
    assert found(
        run(with_collection("kbb"), cat),
        "V014",
        "collection 'kbb' is not defined — collections: kb",
    )
    ok = run(with_collection("kb"), cat)
    assert not ok.errors, [str(e) for e in ok.errors]
    unscanned = run(with_collection("kbb"))  # no collections scanned: nothing to check against
    assert not unscanned.errors, [str(e) for e in unscanned.errors]


# ── V039: llm.stream_tokens ─────────────────────────────────────────────────


def _stream(d: dict, *, guardrail: bool = False, **llm: Any) -> None:
    reply = agent(d, "reply")
    reply["llm"].update(stream_tokens=True, **llm)
    if not guardrail:
        reply.pop("guardrails", None)
        reply["routes"].pop("guardrail_violation", None)


def test_v039_streams_the_one_str_output() -> None:
    report = run(lambda d: _stream(d))
    assert not report.errors, [str(e) for e in report.errors]
    assert not run(lambda d: _stream(d, stream_field="text")).errors


def test_v039_an_output_guardrail_could_refuse_text_already_shown() -> None:
    assert found(run(lambda d: _stream(d, guardrail=True)), "V039", "could still refuse")


def test_v039_several_str_outputs_need_a_named_field() -> None:
    def two(d: dict) -> None:
        _stream(d)
        agent(d, "reply")["outputs"]["subject"] = "str"

    assert found(run(two), "V039", "must say which output to stream")

    def named(d: dict) -> None:
        two(d)
        agent(d, "reply")["llm"]["stream_field"] = "subject"

    assert not [e for e in run(named).errors if e.code == "V039"]


@pytest.mark.parametrize(
    ("field", "fragment"),
    [("nope", "is not an output of 'reply'"), ("amount", "not str")],
)
def test_v039_the_field_must_be_a_str_output(field: str, fragment: str) -> None:
    def m(d: dict) -> None:
        _stream(d, stream_field=field)
        agent(d, "reply")["outputs"]["amount"] = "int"

    assert found(run(m), "V039", fragment)


def test_a_stream_field_without_stream_tokens_is_rejected_by_the_schema() -> None:
    def m(d: dict) -> None:
        agent(d, "reply")["llm"]["stream_field"] = "text"

    with pytest.raises(Exception, match="stream_field"):
        run(m)
