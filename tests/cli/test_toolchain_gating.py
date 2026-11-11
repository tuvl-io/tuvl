"""The bare `tuvl` install exposes the production commands only; the toolchain needs `tuvl[standard]`."""

import re
import subprocess
import sys

import pytest

from tuvl.cli import TOOLCHAIN_COMMANDS
from tuvl.cli.verbs import VERBS
from tuvl.extras import toolchain_installed

# Every verb is listed on a bare install (its toolchain things answer with the install hint);
# the one-word toolchain commands are not.
VERB_NAMES = {verb for verb, _, _ in VERBS}
ONE_WORD_TOOLCHAIN = {"init", "dev", "ship", "deploy"}
CORE = VERB_NAMES | {"help", "iam"}


def _tuvl(*args: str, toolchain: str) -> subprocess.CompletedProcess[str]:
    env = {"TUVL_TOOLCHAIN": toolchain, "PATH": "/usr/bin:/bin", "NO_COLOR": "1", "COLUMNS": "200"}
    return subprocess.run(
        [sys.executable, "-c", "from tuvl.cli import main; main()", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _listed(help_text: str) -> set[str]:
    return set(re.findall(r"^│ (\w+)\s", help_text, flags=re.M))


def test_env_override_forces_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TUVL_TOOLCHAIN", "0")
    assert toolchain_installed() is False
    monkeypatch.setenv("TUVL_TOOLCHAIN", "1")
    assert toolchain_installed() is True


def test_bare_install_lists_the_verbs_but_not_the_one_word_toolchain_commands() -> None:
    out = _tuvl("--help", toolchain="0")
    assert out.returncode == 0
    assert _listed(out.stdout) == CORE
    assert not (ONE_WORD_TOOLCHAIN & _listed(out.stdout))
    assert "tuvl[standard]" in out.stdout


@pytest.mark.parametrize(
    ("words", "shown"),
    [
        (["lint", "project"], "lint project"),
        (["generate", "spec"], "generate spec"),
        (["approve", "spec"], "approve spec"),
        (["review", "code"], "review code"),
        (["run", "tests"], "run tests"),
        (["iam", "export"], "iam export"),
        (["replay", "runs"], "replay runs"),
        (["register", "project"], "register project"),
    ],
)
def test_a_toolchain_thing_under_a_verb_gets_the_install_hint(words: list[str], shown: str) -> None:
    out = _tuvl(*words, "--whatever", toolchain="0")
    assert out.returncode == 2 and f"`tuvl {shown}`" in out.stderr
    assert "tuvl[standard]" in out.stderr


@pytest.mark.parametrize(
    "words",
    [
        ["serve", "app", "--help"], ["check", "system", "--help"], ["check", "storage", "--help"],
        ["generate", "key", "--help"], ["generate", "rls", "--help"], ["verify", "rls", "--help"],
        ["list", "runs", "--help"], ["show", "run", "--help"], ["follow", "run", "--help"],
        ["run", "workflow", "--help"], ["pin", "run", "--help"],
        ["list", "objects", "--help"],
        ["iam", "roles", "list", "--help"], ["iam", "users", "create", "--help"],
        ["iam", "check", "--help"], ["iam", "reset-admin", "--help"],
    ],
)  # fmt: skip
def test_the_production_commands_work_without_the_toolchain(words: list[str]) -> None:
    out = _tuvl(*words, toolchain="0")
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize("name", TOOLCHAIN_COMMANDS)
def test_bare_install_answers_a_toolchain_command_with_the_install_hint(name: str) -> None:
    out = _tuvl(name, "--whatever", toolchain="0")
    assert out.returncode == 2
    assert f"`tuvl {name}`" in out.stderr
    assert "tuvl[standard]" in out.stderr


@pytest.mark.requires_insight
def test_toolchain_install_lists_every_command() -> None:
    out = _tuvl("--help", toolchain="1")
    assert out.returncode == 0
    assert _listed(out.stdout) >= (CORE | ONE_WORD_TOOLCHAIN)
    assert not ({"validate", "codegen", "spec", "runs", "audit"} & _listed(out.stdout))  # hidden
    assert "tuvl[standard]" not in out.stdout


def test_core_commands_work_without_the_toolchain() -> None:
    out = _tuvl("check", "system", "--help", toolchain="0")
    assert out.returncode == 0
    assert "--project-dir" in out.stdout
