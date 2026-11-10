import yaml

from tuvl.cli.commands.init import _SAMPLE_TRIAGE_SPEC, _sample_task_plan
from tuvl_insight.spec.derive import _key, derive, dump, merge
from tuvl_insight.spec.grammar import parse_structure
from tuvl_insight.spec.parse import parse_text

TWO_FLOWS = """---
name: kb
workflow: ask
---
# Data
- **Doc** (new) — `title` (string; required)
- **Old** (existing) — `x` (string)

# Flows

## Flow: ingest
Trigger: POST /ingest · signed-in
Input: `title` (str)
Ends: default 200 · failed 500

1. `fetch` — tool http get https://example.org · in `title` · out `body` (str)
   - default → `save`
2. `save` — tool db create Doc · in `body` · out `doc` (Doc)
   - default → END

## Flow: ask
Trigger: POST /ask · signed-in
Input: `q` (str)
Ends: default 200 · failed 500

1. `answer` — llm default · in `q` · out `a` (str)
   - default → END

# Acceptance
```tuvl-example
name: answers a question
input: { q: hi }
mocks: { answer: { outputs: { a: hello } } }
expect: { end: default }
```
```tuvl-example
name: ingests
workflow: ingest
input: { title: t }
mocks: { fetch: { outputs: { body: b } } }
expect: { end: default }
```
"""


def load(text: str, path: str = "specs/x.md"):
    return parse_text(text, path=path, default_name="x"), parse_structure(text)


def keys(tasks) -> set:
    return {(t["type"], tuple(sorted(t["target"].items()))) for t in tasks}


def test_derivation_matches_the_agent_written_sample_plan() -> None:
    spec, structure = load(_SAMPLE_TRIAGE_SPEC, "specs/triage-ticket.md")
    mine = {d.key for d in derive(spec, structure)}
    theirs = keys(yaml.safe_load(_sample_task_plan())["spec"]["tasks"])
    assert mine == theirs


def test_tasks_cover_models_steps_examples_and_external_systems() -> None:
    spec, structure = load(TWO_FLOWS)
    tasks = {d.key: d for d in derive(spec, structure)}
    kinds = sorted(k[0] for k in tasks)
    assert kinds.count("model") == 1  # only the (new) model
    assert kinds.count("agent") == 3
    assert kinds.count("test") == 2
    assert kinds.count("manual") == 1  # fetch calls http
    manual = next(d for d in tasks.values() if d.type == "manual")
    assert manual.target == {"workflow": "ingest", "agent": "fetch"}


def test_dependencies_follow_the_routes_and_the_models() -> None:
    spec, structure = load(TWO_FLOWS)
    tasks = {d.key: d for d in derive(spec, structure)}
    fetch = tasks[("agent", (("agent", "fetch"), ("workflow", "ingest")))]
    save = tasks[("agent", (("agent", "save"), ("workflow", "ingest")))]
    assert fetch.deps == [("model", (("model", "Doc"),))]  # first step waits for the models
    assert save.deps == [fetch.key]  # `fetch` routes to `save`


def test_an_example_runs_the_workflow_it_names_else_the_front_matter_one() -> None:
    spec, structure = load(TWO_FLOWS)
    by_name = {
        d.target["example"]: d.target["workflow"]
        for d in derive(spec, structure)
        if d.type == "test"
    }
    assert by_name == {"answers a question": "ask", "ingests": "ingest"}


def test_merge_gives_stable_ids_and_section_hashes() -> None:
    spec, structure = load(TWO_FLOWS)
    first = merge(spec, structure, None)
    ids_first = {_key(t): t["id"] for t in first["spec"]["tasks"]}
    assert sorted(ids_first.values()) == [f"T{n}" for n in range(1, len(ids_first) + 1)]
    again = merge(spec, structure, yaml.safe_load(dump(first)))
    assert {_key(t): t["id"] for t in again["spec"]["tasks"]} == ids_first
    model = next(t for t in again["spec"]["tasks"] if t["type"] == "model")
    assert (
        model["spec_ref"]["anchor"] == "data"
        and model["spec_ref"]["hash"] == spec.section("data").hash
    )
    assert again["metadata"]["spec_hash"] == spec.hash


def test_a_removed_step_becomes_obsolete_and_a_new_one_gets_the_next_id() -> None:
    spec, structure = load(TWO_FLOWS)
    plan = yaml.safe_load(dump(merge(spec, structure, None)))
    smaller = TWO_FLOWS.replace(
        "2. `save` — tool db create Doc · in `body` · out `doc` (Doc)\n   - default → END",
        "2. `store` — tool db create Doc · in `body` · out `doc` (Doc)\n   - default → END",
    ).replace("   - default → `save`", "   - default → `store`")
    spec2, structure2 = load(smaller)
    out = merge(spec2, structure2, plan)
    by_agent = {t["target"].get("agent"): t for t in out["spec"]["tasks"] if t["type"] == "agent"}
    assert by_agent["save"]["obsolete"] is True
    assert "obsolete" not in by_agent["store"]
    assert (
        int(by_agent["store"]["id"][1:]) == max(int(t["id"][1:]) for t in plan["spec"]["tasks"]) + 1
    )


def test_manual_tasks_someone_added_survive() -> None:
    spec, structure = load(TWO_FLOWS)
    plan = yaml.safe_load(dump(merge(spec, structure, None)))
    plan["spec"]["tasks"].append(
        {"id": "T99", "title": "Get the vendor contract", "type": "manual", "status": "done"}
    )
    out = merge(spec, structure, plan)
    kept = next(t for t in out["spec"]["tasks"] if t["id"] == "T99")
    assert kept["status"] == "done" and "obsolete" not in kept


def test_a_numbered_spec_gets_numbered_tasks() -> None:
    text = TWO_FLOWS.replace("name: kb\n", "name: kb\nid: SPEC-SK-0004-01\n")
    spec, structure = load(text)
    out = merge(spec, structure, None)
    task_ids = [t["id"] for t in out["spec"]["tasks"]]
    assert (
        task_ids[0] == "TASK-SK-0004-01-001"
        and task_ids[-1] == f"TASK-SK-0004-01-{len(task_ids):03d}"
    )
    deps = [d for t in out["spec"]["tasks"] for d in t.get("depends_on", [])]
    assert deps and all(d.startswith("TASK-SK-0004-01-") for d in deps)


def test_the_dumped_plan_has_no_yaml_aliases() -> None:
    spec, structure = load(TWO_FLOWS)
    assert "&id" not in dump(merge(spec, structure, None))
