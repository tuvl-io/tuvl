"""A stand-in coding agent for the tests: reads the prompt on stdin and edits the working directory.

FAKE_MODE: good | invalid | invalid_then_good | outside | outside_then_good | approve | fail | hello
  (codegen tasks) good | break | break_then_implement | stray_then_implement
FAKE_STATE: a file counting its runs; FAKE_PROMPTS: a file every prompt is appended to.
"""

import os
import re
import sys
from pathlib import Path

prompt = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "good")
state = Path(os.environ["FAKE_STATE"]) if os.environ.get("FAKE_STATE") else None
run = (int(state.read_text()) if state and state.exists() else 0) + 1
if state:
    state.write_text(str(run))
if os.environ.get("FAKE_PROMPTS"):
    with open(os.environ["FAKE_PROMPTS"], "a") as fh:
        fh.write(prompt + "\n=====\n")

if "You are reviewing Python written for a tuvl project" in prompt:
    import json

    verdict = os.environ.get("FAKE_REVIEW", "clean")
    if verdict == "garbage":
        print("I think the code looks fine overall.")
    elif verdict == "writes":
        Path("agents").mkdir(exist_ok=True)
        Path("agents/injected.py").write_text("print('hi')\n")
        print(json.dumps({"verdict": "pass", "findings": []}))
    elif verdict == "concerns":
        print(
            "Here is my review:\n"
            + json.dumps(
                {
                    "verdict": "concerns",
                    "findings": [
                        {
                            "file": "agents/x.py",
                            "line": 3,
                            "severity": "high",
                            "rule": "Money Float",
                            "summary": "uses float for money",
                            "suggestion": "use Decimal",
                        },
                        {
                            "file": "agents/x.py",
                            "line": 9,
                            "severity": "low",
                            "rule": "no-example",
                            "summary": "no example reaches this branch",
                        },
                    ],
                }
            )
        )
    else:
        print(json.dumps({"verdict": "pass", "findings": []}))
    sys.exit(0)

if mode == "fail":
    print("the model is overloaded", file=sys.stderr)
    sys.exit(3)
if "# The step in the spec" in prompt:  # a codegen task, not a spec draft
    import yaml

    step = re.search(r"step `(\w+)` is still", prompt).group(1)
    wf_path = Path(re.search(r"is in `(workflows/[\w.-]+\.yaml)`", prompt).group(1))
    model = re.search(rf"`{step}` — tool db create (\w+)", prompt).group(1)
    doc = yaml.safe_load(wf_path.read_text())
    agent = next(a for a in doc["spec"]["agents"] if a["id"] == step)
    if mode == "break" or (mode == "break_then_implement" and run == 1):
        agent["routes"] = {**agent["routes"], "bogus": "END"}  # contradicts the spec
    else:
        agent["routes"].pop("bogus", None)
        agent["engine"] = "tool"
        agent["tool"] = {
            "use": "db",
            "model": model,
            "op": "create",
            "values": {k: "{{ " + k + " }}" for k in agent["inputs"]},
        }
    wf_path.write_text(yaml.safe_dump(doc, sort_keys=False))
    if mode == "stray_then_implement" and run == 1:
        Path("specs").mkdir(exist_ok=True)
        Path("specs/stray.md").write_text("changed by the agent\n")
    print("implemented " + step)
    sys.exit(0)

if mode == "hello":
    Path("hello.txt").write_text("ok")
    print("done")
    sys.exit(0)

match = re.search(r"`(specs/SPEC-[A-Z0-9-]+\.md)`", prompt)
if not match:
    print("no spec path in the prompt", file=sys.stderr)
    sys.exit(2)
spec = Path(match.group(1))
text = spec.read_text()

if mode == "good" or (mode.endswith("_then_good") and run > 1):
    text = text.replace("\n# Flows\nnot a flow\n", "")  # fixes what the previous attempt broke
    spec.write_text(text.replace("# Intent\n", "# Intent\nRefund customers within two days.\n", 1))
elif mode.startswith("invalid"):
    spec.write_text(text + "\n# Flows\nnot a flow\n")
elif mode.startswith("outside"):
    spec.write_text(text.replace("# Intent\n", "# Intent\nDrafted.\n", 1))
    Path("models").mkdir(exist_ok=True)
    Path("models/sneaky.yaml").write_text("kind: Model\n")
    Path("AGENTS.md").write_text("changed by the agent\n")
elif mode == "approve":
    spec.write_text(
        text.replace("status: draft", "status: approved").replace(
            "---\n", "---\napproved_by: me\napproved_hash: abc\n", 1
        )
    )
print("wrote the draft")
