"""Skills (P3.9): bundled content, install/update, legacy removal, CLI."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import yaml
from typer.testing import CliRunner

from tuvl import skills
from tuvl.cli import app
from tuvl.contract.schema import Effect

EXPECTED = {
    "tuvl-spec",
    "tuvl-plan",
    "tuvl-task-loop",
    "agent-code",
    "agent-tool",
    "agent-llm",
    "agent-loop",
    "agent-decide",
    "agent-human",
    "agent-map",
    "agent-parallel",
    "tuvl-data",
    "tuvl-test",
    "tuvl-harden",
    "tuvl-review",
    "tuvl-ship",
    "tuvl-workflow",
    "tuvl-ui",
    "ui-run",
    "ui-approve",
    "ui-auth",
    "ui-data",
}
runner = CliRunner()


def test_the_bundled_skills_have_front_matter() -> None:
    assert set(skills.names()) == EXPECTED
    for path, text in skills.bundled().items():
        if path.name != "SKILL.md":
            continue
        front = yaml.safe_load(text.split("---")[1])
        assert front["name"] == path.parent.name and len(front["description"]) > 30


def test_skills_only_mention_real_commands() -> None:
    commands = {c.name for c in app.registered_commands} | {g.name for g in app.registered_groups}
    for path, text in skills.bundled().items():
        for used in re.findall(r"`tuvl ([a-z][a-z-]*)", text):
            assert used in commands, f"{path} mentions `tuvl {used}`"


def test_the_agent_code_skill_lists_exactly_the_effects_the_schema_accepts() -> None:
    text = next(
        t
        for p, t in skills.bundled().items()
        if p.name == "SKILL.md" and p.parent.name == "agent-code"
    )
    listed = re.search(r"effect: \w+\s+# ([a-z |]+?);", text)
    assert listed, "the effect list comment moved"
    assert {v.strip() for v in listed.group(1).split("|")} == set(get_args(Effect))


def test_install_update_and_legacy_removal(tmp_path: Path) -> None:
    (tmp_path / ".agents/skills/create-database-model").mkdir(parents=True)
    (tmp_path / ".agents/skills/my-own-skill").mkdir()
    (tmp_path / "CLAUDE.md").write_text("Project notes.\n")
    changes = skills.install(tmp_path)
    assert {c.action for c in changes if c.path.name == "create-database-model"} == {"delete"}
    assert not (tmp_path / ".agents/skills/create-database-model").exists()
    assert (tmp_path / ".agents/skills/my-own-skill").exists()  # not ours: untouched
    assert (tmp_path / ".claude/skills/tuvl-task-loop/SKILL.md").is_file()
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\n\nProject notes.\n"
    assert skills.plan(tmp_path) == []  # idempotent
    (tmp_path / "AGENTS.md").write_text("old rules\n")
    assert [(str(c.path), c.action) for c in skills.plan(tmp_path)] == [("AGENTS.md", "update")]


def test_cli_diff_writes_nothing(tmp_path: Path) -> None:
    preview = runner.invoke(app, ["update", "skills", "-d", str(tmp_path), "--diff"])
    assert (
        preview.exit_code == 0
        and "AGENTS.md" in preview.output
        and not (tmp_path / "AGENTS.md").exists()
    )
    assert runner.invoke(app, ["update", "skills", "-d", str(tmp_path)]).exit_code == 0
    assert "up to date" in runner.invoke(app, ["update", "skills", "-d", str(tmp_path)]).output


def test_init_installs_the_skills(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path / "p"), "-y"])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (tmp_path / "p/.agents/skills").iterdir()) == sorted(EXPECTED)
    assert "list tasks" in (tmp_path / "p/AGENTS.md").read_text()


#: What `@tuvl/client` 2.x exports (src/index.ts); a skill may import only these.
CLIENT_EXPORTS = {
    "RunHandle", "TuvlClient", "isTokenEvent", "TuvlAuth", "readToken", "tokenExpiresAt", "isTokenExpired",
    "CrudClient", "WebhookError", "verifyWebhook", "webhookHandler", "parseSseEvents", "parseSseStream",
    "Transport", "TERMINAL_STATUSES", "TuvlApiError", "TuvlRunError", "WAITING_STATUSES",
}  # fmt: skip


def test_skills_import_only_what_the_client_exports() -> None:
    for path, text in skills.bundled().items():
        for block in re.findall(r"import (?:type )?\{([^}]*)\} from \"@tuvl/client\"", text):
            for name in (n.strip() for n in block.split(",") if n.strip()):
                assert name in CLIENT_EXPORTS, f"{path} imports {name} from @tuvl/client"
