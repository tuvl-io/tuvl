"""Write docs/cli-reference.md: every command and every option of the CLI, read from the CLI itself.

    python scripts/gen_cli_reference.py          # rewrite the page
    python scripts/gen_cli_reference.py --check  # exit 1 if the page is out of date

The hand-written docs/cli.md says what each command is for; this page lists what it takes, so a new option
cannot be missing from the docs. tests/cli/test_cli_reference.py fails when this page is stale.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import typer

PAGE = Path(__file__).resolve().parent.parent / "docs" / "cli-reference.md"
SKIP = {"help"}
VERB_ORDER = [
    "init",
    "dev",
    "ship",
    "deploy",
    "create",
    "import",
    "use",
    "set",
    "upgrade",
    "register",
    "update",
    "generate",
    "approve",
    "reject",
    "reopen",
    "renumber",
    "lint",
    "verify",
    "check",
    "review",
    "run",
    "serve",
    "follow",
    "pin",
    "replay",
    "list",
    "show",
    "export",
    "prune",
]

HEADER = """# CLI reference (every command and option)

This page is generated from the command line itself (`python scripts/gen_cli_reference.py`), so it lists every
command and every option that exists. [CLI reference](cli.md) says what each command is for.

- `-d/--project-dir` defaults to the current directory on every command that works on a project.
- `-u/--url` (`TUVL_URL`) and `-t/--token` (`TUVL_TOKEN`) are how the commands that talk to a running server find it.
- Options written `--x/--no-x` are on/off switches; the one shown in **Default** is on unless you switch it.
- Commands marked *toolchain* need the developer toolchain (`uv tool install "tuvl[standard]"`).
"""

TOOLCHAIN = {"init", "dev", "ship", "deploy"}
TOP_LEVEL_HEADING = TOOLCHAIN


def _is_group(cmd: Any) -> bool:
    return hasattr(cmd, "list_commands")


def _clean(text: str) -> str:
    text = re.sub(r"\[/?[a-z ]+\]", "", text or "")  # rich markup
    return " ".join(text.split()).replace("|", "\\|")


def _commands() -> dict[str, Any]:
    from typer._click.core import Context

    from tuvl.cli import app

    root = typer.main.get_command(app)
    found: dict[str, Any] = {}

    def walk(cmd: Any, path: list[str], ctx: Any) -> None:
        if _is_group(cmd):
            for name in sorted(cmd.list_commands(ctx)):
                sub = cmd.get_command(ctx, name)
                if sub is not None and not getattr(sub, "hidden", False):
                    walk(sub, [*path, name], Context(sub, parent=ctx, info_name=name))
        elif path and path[-1] not in SKIP:
            found[" ".join(path)] = cmd

    walk(root, [], Context(root, info_name="tuvl"))
    return found


def _option_row(p: Any) -> tuple[str, str, str, str] | None:
    flags = [f for f in [*p.opts, *getattr(p, "secondary_opts", [])] if f.startswith("-")]
    if not flags or flags[0] in ("-h", "--help"):
        return None
    shown = ", ".join(f"`{f}`" for f in sorted(p.opts, key=lambda f: (not f.startswith("--"), f)))
    if getattr(p, "secondary_opts", None):
        shown = (
            "`"
            + "/".join(p.opts[-1:] + p.secondary_opts[:1])
            + "`"
            + (", " + ", ".join(f"`{f}`" for f in p.opts[:-1]) if len(p.opts) > 1 else "")
        )
    kind = getattr(p.type, "name", "").upper()
    choices = list(getattr(p.type, "choices", []) or [])
    if getattr(p, "is_flag", False) or kind == "BOOL":
        value = ""
    elif choices:
        value = "|".join(str(c) for c in choices)
    else:
        value = {"TEXT": "TEXT", "STRING": "TEXT"}.get(kind, kind)
    if getattr(p, "multiple", False):
        value += " (repeatable)"
    default = getattr(p, "default", None)
    if default in (None, (), [], "") or (
        default is False and not getattr(p, "secondary_opts", None)
    ):
        shown_default = ""
    elif default is False:
        shown_default = "off"
    elif default is True:
        shown_default = "on"
    else:
        shown_default = f"`{default}`"
    env = getattr(p, "envvar", None)
    note = _clean(getattr(p, "help", "") or "")
    if env:
        note += f" (`{env if isinstance(env, str) else env[0]}`)"
    return shown, value, shown_default, note


def render() -> str:
    cmds = _commands()
    by_verb: dict[str, list[str]] = {}
    for path in cmds:
        by_verb.setdefault(path.split()[0], []).append(path)
    verbs = [v for v in VERB_ORDER if v in by_verb] + sorted(
        v for v in by_verb if v not in VERB_ORDER
    )
    out = [HEADER]
    top_level_printed = False
    for verb in verbs:
        single = by_verb[verb] == [verb]
        if single and verb in TOP_LEVEL_HEADING:
            if not top_level_printed:
                out.append("\n## Commands without a verb\n")
                top_level_printed = True
        elif not single:
            out.append(f"\n## `tuvl {verb}`\n")
        for path in by_verb[verb]:
            cmd = cmds[path]
            tag = " *(toolchain)*" if path in TOOLCHAIN else ""
            args = [
                f"<{p.name}>" if p.required else f"[{p.name}]"
                for p in cmd.params
                if getattr(p, "param_type_name", "") == "argument"
            ]
            out.append(f"\n### `tuvl {path}`{tag}\n")
            out.append(
                _clean((cmd.help or getattr(cmd, "short_help", "") or "").split("\n\n")[0]) + "\n"
            )
            if args:
                out.append("Arguments: " + " ".join(f"`{a}`" for a in args) + "\n")
            rows = [
                r
                for r in (
                    _option_row(p)
                    for p in cmd.params
                    if getattr(p, "param_type_name", "") == "option"
                )
                if r
            ]
            if rows:
                out.append("| Option | Value | Default | What it does |\n|---|---|---|---|")
                out.extend(f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows)
                out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        if not PAGE.exists() or PAGE.read_text() != text:
            print(f"{PAGE} is out of date: run python scripts/gen_cli_reference.py")
            return 1
        return 0
    PAGE.write_text(text)
    print(f"wrote {PAGE} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
