"""Test 2.0 runner (P3.1): mocks, replay, offline pointers and expectations."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

from tuvl import llm as llm_client
from tuvl.contract.schema import TestDocument, WorkflowDocument
from tuvl.core.artifacts import registry as artifacts
from tuvl.core.artifacts.registry import ArtifactEntry, ArtifactVersion
from tuvl.runtime import judges
from tuvl.runtime.code import AGENT_REGISTRY
from tuvl_insight.testing import run_test


async def _score(inp: Any, ctx: Any) -> tuple[dict[str, Any], str]:
    return {"score": inp.amount * 2}, ("big" if inp.amount > 50 else "small")


@pytest.fixture(autouse=True)
def agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(AGENT_REGISTRY, "score", _score)


WF = WorkflowDocument.model_validate(
    {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "wf"},
        "spec": {
            "trigger": {"input": {"text": "str"}},
            "agents": [
                {
                    "id": "extract",
                    "description": "read the amount",
                    "engine": "llm",
                    "inputs": {"text": "str"},
                    "outputs": {"amount": "int"},
                    "budget": {"timeout": "10s", "max_tokens": 100},
                    "llm": {"model": "openai/x", "prompt": "Extract."},
                    "routes": {
                        "default": "route",
                        "error": "END.failed",
                        "parse_error": "END.failed",
                        "timeout": "END.failed",
                        "budget_exceeded": "END.failed",
                    },
                },
                {
                    "id": "route",
                    "description": "route by size",
                    "engine": "decide",
                    "inputs": {"amount": "int"},
                    "outputs": {"lane": "enum[fast, slow]"},
                    "decide": {
                        "rules": [
                            {"when": "amount < 10", "then": "fast"},
                            {"when": "amount >= 10", "then": "slow"},
                        ]
                    },
                    "routes": {"fast": "score", "slow": "score", "error": "END.failed"},
                },
                {
                    "id": "score",
                    "description": "score it",
                    "engine": "code",
                    "code": {"run": "score"},
                    "inputs": {"amount": "int"},
                    "outputs": {"score": "int"},
                    "routes": {"big": "END.big", "small": "END", "error": "END.failed"},
                },
            ],
            "outputs": {
                "default": {"body": {"score": "{{ score }}", "kind": "small"}},
                "big": {"status": 201, "body": {"score": "{{ score }}", "kind": "big"}},
                "failed": {"status": 500},
            },
        },
    }
)


def test_doc(**spec: Any) -> TestDocument:
    return TestDocument.model_validate(
        {
            "kind": "Test",
            "version": "tuvl/v2",
            "metadata": {"name": "t"},
            "spec": {"workflow": "wf", "input": {"text": "x"}, **spec},
        }
    )


async def run(test: TestDocument, tmp_path: Path, **kw: Any) -> Any:
    return await run_test(test, {"wf": WF}, base=tmp_path, models={}, **kw)


async def test_mocks_and_expectations_pass(tmp_path: Path) -> None:
    result = await run(
        test_doc(
            mocks={"extract": {"outputs": {"amount": 60}}},
            expect={
                "end": "big",
                "path": ["extract", "route", "score"],
                "decisions": {"route": {"route": "slow", "source": "rule#2"}},
                "output": {"status": 201, "body": {"kind": "big"}},
                "assertions": ["output.body.score == 120", "end == 'big'"],
            },
        ),
        tmp_path,
    )
    assert result.passed, result.failures


async def test_each_kind_of_mismatch_is_reported(tmp_path: Path) -> None:
    result = await run(
        test_doc(mocks={"extract": {"outputs": {"amount": 3}}}, expect={"end": "big"}), tmp_path
    )
    assert not result.passed and result.failures == ["end: expected 'big', got 'default'"]

    result = await run(
        test_doc(
            mocks={"extract": {"outputs": {"amount": 3}}},
            expect={"output": {"body": {"score": 7}}, "assertions": ["output.body.score > 100"]},
        ),
        tmp_path,
    )
    assert "output.body.score: expected 7, got 6" in result.failures
    assert "assertion failed: output.body.score > 100" in result.failures


async def test_stale_mock_fails_the_contract(tmp_path: Path) -> None:
    result = await run(
        test_doc(mocks={"extract": {"outputs": {"amount": "lots"}}}, expect={"end": "big"}),
        tmp_path,
    )
    assert not result.passed and result.end == "failed"


async def test_unmocked_model_agent_points_at_mock_or_live(tmp_path: Path) -> None:
    result = await run(test_doc(expect={"end": "big"}), tmp_path)
    assert not result.passed
    assert (
        "'extract' (llm) is not mocked — add mocks.extract, a replay:, or run with --live"
        in result.failures[0]
    )


async def test_replay_serves_recorded_results(tmp_path: Path) -> None:
    journal = {
        "format": "tuvl.journal/v1",
        "run": {"workflow": "wf", "input": {"text": "x"}},
        "events": [],
        "checkpoints": [
            {
                "seq": 4,
                "agent_id": "extract",
                "signal": "default",
                "next": "route",
                "context": {"text": "x", "amount": 70},
                "completed": ["extract"],
            }
        ],
    }
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "rec.journal.json").write_text(json.dumps(journal))
    result = await run(
        test_doc(
            replay="fixtures/rec.journal.json",
            expect={"end": "big", "output": {"body": {"score": 140}}},
        ),
        tmp_path,
    )
    assert result.passed, result.failures and result.tokens == 0


async def test_judge_expectations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judges, "_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setitem(
        artifacts.ARTIFACT_REGISTRY,
        "tone",
        ArtifactEntry(
            "tone",
            "judge",
            versions={
                1: ArtifactVersion(
                    content={"model": "openai/j", "rubric": "polite"},
                    content_hash="h",
                    source="file",
                )
            },
        ),
    )
    verdict = {"v": "pass"}

    async def judge_model(**kwargs: Any) -> Any:
        content = json.dumps({"verdict": verdict["v"], "score": 0.9, "reasons": ["r"]})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(total_tokens=5),
        )

    monkeypatch.setattr(llm_client, "acompletion", judge_model)
    expect = {"judge": [{"judge": "artifact://tone@1", "target": "{{ output.body }}"}]}
    mocks = {"extract": {"outputs": {"amount": 3}}}
    assert (await run(test_doc(mocks=mocks, expect=expect), tmp_path)).passed
    verdict["v"] = "uncertain"
    other = {**mocks, "extract": {"outputs": {"amount": 4}}}  # a new subject: not cached
    assert not (await run(test_doc(mocks=other, expect=expect), tmp_path)).passed
    assert (await run(test_doc(mocks=other, expect=expect), tmp_path, allow_uncertain=True)).passed


async def test_unmocked_human_suspends_and_wait_asserts_it(tmp_path: Path) -> None:
    wf = WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "wf"},
            "spec": {
                "agents": [
                    {
                        "id": "ok",
                        "description": "approve",
                        "engine": "human",
                        "outputs": {"yes": "bool"},
                        "human": {"title": "Approve?", "route_on": "yes"},
                        "routes": {"true": "END", "false": "END.no", "error": "END"},
                    }
                ],
                "outputs": {"default": {}, "no": {"status": 409}},
            },
        }
    )

    def doc(**expect: Any) -> TestDocument:
        return TestDocument.model_validate(
            {
                "kind": "Test",
                "version": "tuvl/v2",
                "metadata": {"name": "h"},
                "spec": {"workflow": "wf", **expect},
            }
        )

    waited = await run_test(doc(expect={"wait": "approval"}), {"wf": wf}, base=tmp_path, models={})
    assert waited.passed and waited.waiting_at == "ok"  # counts as covering the human agent
    unexpected = await run_test(doc(expect={"end": "no"}), {"wf": wf}, base=tmp_path, models={})
    assert "run stopped waiting_human at ok — mock it, or expect wait:" in unexpected.failures
    mocked = doc(
        mocks={"ok": {"outputs": {"yes": False}, "signal": "false"}},
        expect={"end": "no", "path_includes": ["ok"]},
    )
    assert (await run_test(mocked, {"wf": wf}, base=tmp_path, models={})).passed


# ── honest results, given/as, mock policy (spec-referee P1/P1b) ─────────────


def _with_pending_score() -> WorkflowDocument:
    data = WF.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data["spec"]["agents"][2] = {
        "id": "score",
        "description": "score it",
        "engine": "pending",
        "inputs": {"amount": "int"},
        "outputs": {"score": "int"},
        "routes": {"default": "END"},
    }
    return WorkflowDocument.model_validate(data)


async def test_a_path_through_a_pending_agent_is_stubbed_not_passed(tmp_path: Path) -> None:
    wf = _with_pending_score()
    test = test_doc(mocks={"extract": {"outputs": {"amount": 5}}}, expect={"end": "default"})
    result = await run_test(test, {"wf": wf}, base=tmp_path, models={})
    assert result.passed and result.status == "stubbed" and result.stubbed == ["score"]
    mocked = test_doc(
        mocks={"extract": {"outputs": {"amount": 5}}, "score": {"outputs": {"score": 1}}},
        expect={"end": "default"},
    )
    assert (await run_test(mocked, {"wf": wf}, base=tmp_path, models={})).status == "passed"


async def test_mocking_a_deterministic_agent_warns(tmp_path: Path) -> None:
    result = await run(
        test_doc(
            mocks={
                "extract": {"outputs": {"amount": 5}},
                "score": {"signal": "small", "outputs": {"score": 1}},
            },
            expect={"end": "default"},
        ),
        tmp_path,
    )
    assert result.status == "passed"
    assert (
        len(result.warnings) == 1
        and "T001" in result.warnings[0]
        and "'score'" in result.warnings[0]
    )


class Member(SQLModel, table=True):
    __tablename__ = "runner_test_members"
    id: int | None = SQLField(default=None, primary_key=True)
    user_id: str
    role: str


async def _who(inp: Any, ctx: Any) -> tuple[dict[str, Any], str]:
    rows = await ctx.db.list("Member", user_id=ctx.principal)
    return ({"role": rows[0]["role"]}, "member") if rows else ({"role": "none"}, "stranger")


MEMBERS = WorkflowDocument.model_validate(
    {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "members"},
        "spec": {
            "models": ["Member"],
            "trigger": {"input": {}},
            "agents": [
                {
                    "id": "who",
                    "description": "who is calling",
                    "engine": "code",
                    "effect": "read",
                    "code": {"run": "who"},
                    "outputs": {"role": "str"},
                    "routes": {"member": "END", "stranger": "END.stranger", "error": "END.failed"},
                }
            ],
            "outputs": {
                "default": {"body": {"role": "{{ role }}"}},
                "stranger": {"status": 403},
                "failed": {"status": 500},
            },
        },
    }
)


async def test_given_rows_and_the_as_caller_reach_real_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl.core.models.loader import MODEL_REGISTRY

    monkeypatch.setitem(AGENT_REGISTRY, "who", _who)
    monkeypatch.setitem(MODEL_REGISTRY, "Member", Member)  # ctx.db resolves project models

    def doc(**spec: Any) -> TestDocument:
        return TestDocument.model_validate(
            {
                "kind": "Test",
                "version": "tuvl/v2",
                "metadata": {"name": "m"},
                "spec": {"workflow": "members", **spec},
            }
        )

    given = {"Member": [{"user_id": "u-anu", "role": "manager"}]}
    member = await run_test(
        doc(
            given=given,
            **{"as": {"user": "u-anu"}},
            expect={"end": "default", "output": {"body": {"role": "manager"}}},
        ),
        {"members": MEMBERS},
        base=tmp_path,
        models={"Member": Member},
    )
    assert member.status == "passed", member.failures
    stranger = await run_test(
        doc(given=given, **{"as": {"user": "u-other"}}, expect={"end": "stranger"}),
        {"members": MEMBERS},
        base=tmp_path,
        models={"Member": Member},
    )
    assert stranger.status == "passed", stranger.failures
    unknown = await run_test(
        doc(given={"Ghost": [{"x": 1}]}, expect={"end": "default"}),
        {"members": MEMBERS},
        base=tmp_path,
        models={"Member": Member},
    )
    assert unknown.status == "failed" and "given.Ghost: model is not loaded" in unknown.failures


async def test_an_unexpected_end_names_the_agent_and_its_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(inp: Any, ctx: Any) -> tuple[dict[str, Any], str]:
        raise RuntimeError("ledger offline")

    monkeypatch.setitem(AGENT_REGISTRY, "score", broken)
    result = await run(
        test_doc(mocks={"extract": {"outputs": {"amount": 60}}}, expect={"end": "big"}), tmp_path
    )
    assert any(
        f.startswith("end: expected 'big', got 'failed'") and "`score` emitted error" in f
        for f in result.failures
    ), result.failures


async def test_a_rollback_records_its_compensators_and_fault_injection_is_not_t001(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def step(inp: Any, ctx: Any) -> dict[str, Any]:
        return {"n": 1}

    async def undo(inp: Any, ctx: Any) -> dict[str, Any]:
        return {"undone": True}

    monkeypatch.setitem(AGENT_REGISTRY, "write_step", step)
    monkeypatch.setitem(AGENT_REGISTRY, "undo_step", undo)
    wf = WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "rb"},
            "spec": {
                "trigger": {"input": {}},
                "agents": [
                    {
                        "id": "write",
                        "description": "w",
                        "engine": "code",
                        "effect": "write",
                        "code": {"run": "write_step"},
                        "outputs": {"n": "int"},
                        "compensate": "undo",
                        "routes": {"default": "next", "error": "END.failed"},
                    },
                    {
                        "id": "next",
                        "description": "n",
                        "engine": "code",
                        "effect": "read",
                        "code": {"run": "write_step"},
                        "outputs": {"n": "int"},
                        "routes": {"default": "END", "error": "END.failed"},
                    },
                    {
                        "id": "undo",
                        "description": "u",
                        "engine": "code",
                        "effect": "write",
                        "code": {"run": "undo_step"},
                        "inputs": {"n": "int"},
                        "outputs": {"undone": "bool"},
                        "routes": {"default": "END"},
                    },
                ],
                "outputs": {"default": {}, "failed": {"status": 500, "rollback": True}},
            },
        }
    )
    test = TestDocument.model_validate(
        {
            "kind": "Test",
            "version": "tuvl/v2",
            "metadata": {"name": "rb"},
            "spec": {
                "workflow": "rb",
                "mocks": {"next": {"signal": "error"}},
                "expect": {"end": "failed"},
            },
        }
    )
    result = await run_test(test, {"rb": wf}, base=tmp_path, models={})
    assert result.status == "passed", result.failures
    assert result.compensated == ["undo"] and result.warnings == []
