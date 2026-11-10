import textwrap

import pytest

from tuvl_insight.review.static import AgentInfo, review_source

HEAD = "from tuvl import Ctx, agent\n"


def codes(src: str, agents: dict[str, AgentInfo] | None = None) -> list[str]:
    found = review_source(HEAD + textwrap.dedent(src), "agents/a.py", agents=agents)
    return sorted({f.code for f in found})


def body(*lines: str, effect: str = "write") -> tuple[str, dict[str, AgentInfo]]:
    code = '@agent("a")\nasync def a(inp, ctx):\n' + "".join(f"    {ln}\n" for ln in lines)
    return code, {"a": AgentInfo("a", "w", "step", effect, {"ok", "high", "default", "error"})}


def flagged(*lines: str, effect: str = "write", prefix: str = "") -> list[str]:
    code, agents = body(*lines, effect=effect)
    return codes(prefix + code, agents)


def test_a_clean_agent_has_no_findings() -> None:
    assert flagged("total = sum(inp.items)", 'return {"total": total}, "ok"') == []


@pytest.mark.parametrize(
    ("prefix", "lines", "expected"),
    [
        ("import subprocess\n", ["subprocess.run(['ls'])", 'return "ok"'], "C002"),
        ("import os\n", ["os.system('ls')", 'return "ok"'], "C002"),
        ("", ["eval('1+1')", 'return "ok"'], "C002"),
        ("import pickle\n", ["x = pickle.loads(inp.blob)", 'return "ok"'], "C002"),
        ("import yaml\n", ["x = yaml.load(inp.text)", 'return "ok"'], "C002"),
        ("import requests\n", ["requests.get('x')", 'return "ok"'], "C003"),
        ("import httpx as h\n", ["h.get('x')", 'return "ok"'], "C003"),
        ("from urllib.request import urlopen\n", ["urlopen('x')", 'return "ok"'], "C003"),
        ("import os\n", ["k = os.environ['K']", 'return "ok"'], "C004"),
        ("import os\n", ["k = os.getenv('K')", 'return "ok"'], "C004"),
        ("", ["open('f', 'w').write('x')", 'return "ok"'], "C005"),
        ("import shutil\n", ["shutil.rmtree('d')", 'return "ok"'], "C005"),
        ("from pathlib import Path\n", ["Path('f').write_text('x')", 'return "ok"'], "C005"),
        ("import random\n", ["n = random.randint(1, 3)", 'return "ok"'], "C006"),
        ("import uuid\n", ["i = uuid.uuid4()", 'return "ok"'], "C006"),
        ("from datetime import datetime\n", ["t = datetime.now()", 'return "ok"'], "C006"),
        ("import time\n", ["t = time.time()", 'return "ok"'], "C006"),
        ("", ["global CACHE", 'return "ok"'], "C008"),
        ("", ["token = 'AKIAABCDEFGHIJKLMNOP'", 'return "ok"'], "C010"),
        ("", ["sql_password = 'hunter2hunter2'", 'return "ok"'], "C010"),
        ("", ["url = 'https://api.example.com/v1'", 'return "ok"'], "C009"),
        ("", ['return "bogus"'], "C011"),
        ("", ["return {}, 'bogus'"], "C011"),
    ],
)
def test_each_rule_catches_its_case(prefix: str, lines: list[str], expected: str) -> None:
    assert expected in flagged(*lines, prefix=prefix)


def test_reading_a_file_is_only_a_warning() -> None:
    code, agents = body("data = open('f').read()", 'return "ok"')
    found = review_source(HEAD + code, "agents/a.py", agents=agents)
    assert [(f.code, f.severity) for f in found] == [("C005", "warning")]


def test_the_context_is_the_allowed_way_to_do_things() -> None:
    assert (
        flagged(
            "r = await ctx.http.get(inp.url)",
            "row = await ctx.db.get('Item', inp.id)",
            "await ctx.db.create('Item', {'a': 1})",
            'return {"r": r}, "ok"',
        )
        == []
    )


def test_module_level_work_is_flagged() -> None:
    assert "C007" in codes("print('loaded')\n")
    assert "C007" in codes("data = load_all()\n")
    assert codes("import re\nPATTERN = re.compile('x')\nLIMIT = 3\nNAMES = ('a', 'b')\n") == []
    assert codes('"""doc"""\nfrom decimal import Decimal\nTOL = Decimal("0.01")\n') == []


def test_module_state_is_flagged() -> None:
    state = "CACHE = {}\n"
    assert "C008" in flagged("CACHE['k'] = 1", 'return "ok"', prefix=state)
    assert "C008" in flagged("CACHE.update({'k': 1})", 'return "ok"', prefix=state)
    assert "C008" not in flagged(
        "CACHE = {}", "CACHE['k'] = 1", 'return "ok"', prefix=state
    )  # a local of the same name
    assert "C008" not in flagged("seen = {}", "seen['k'] = 1", 'return "ok"')


def test_an_agent_with_the_wrong_shape_is_flagged() -> None:
    assert "C001" in codes('@agent("a")\ndef a(inp, ctx):\n    return "ok"\n')
    assert "C001" in codes('@agent("a")\nasync def a(inp):\n    return "ok"\n')
    assert "C001" in codes('@agent("a")\nasync def a(inp, ctx, extra):\n    return "ok"\n')
    assert "C001" in codes("@agent\nasync def a(inp, ctx):\n    return 'ok'\n")


def test_a_syntax_error_is_reported_not_raised() -> None:
    found = review_source("def broken(:\n", "agents/a.py")
    assert [f.code for f in found] == ["C001"] and "does not parse" in found[0].message


def test_signals_are_checked_against_the_workflows_routes() -> None:
    agents = {"a": AgentInfo("a", "w", "step", "write", {"ok", "high"})}
    src = '@agent("a")\nasync def a(inp, ctx):\n    if inp.x:\n        return {"v": 1}, "high"\n    return "low"\n'
    found = review_source(HEAD + src, "agents/a.py", agents=agents)
    assert [(f.code, f.line) for f in found] == [("C011", 6)]
    assert "routes only ['high', 'ok']" in found[0].message


def test_an_agent_no_workflow_runs_is_noted() -> None:
    assert codes('@agent("orphan")\nasync def a(inp, ctx):\n    return "ok"\n', agents={}) == [
        "C013"
    ]
    assert (
        codes('@agent("orphan")\nasync def a(inp, ctx):\n    return "ok"\n', agents=None) == []
    )  # no workflows to compare


def test_writing_in_a_read_agent_is_flagged() -> None:
    assert "C012" in flagged("await ctx.db.update('Item', 1, {})", 'return "ok"', effect="read")
    assert "C012" not in flagged("await ctx.db.get('Item', 1)", 'return "ok"', effect="read")
    assert "C012" not in flagged(
        "await ctx.db.update('Item', 1, {})", 'return "ok"', effect="write"
    )


def test_a_docstring_with_a_url_is_not_a_hard_coded_endpoint() -> None:
    assert codes('"""See https://docs.example.com/guide for details."""\n') == []


def test_findings_name_the_file_and_line() -> None:
    code, agents = body("os.system('ls')", 'return "ok"')
    found = review_source("import os\n" + HEAD + code, "agents/x.py", agents=agents)
    assert [(f.file, f.line, f.severity) for f in found] == [("agents/x.py", 5, "error")]
