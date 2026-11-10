from __future__ import annotations

import contextlib
import importlib.metadata
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from tuvl.core.config import use_project
from tuvl_insight.cli import laya, select
from tuvl_insight.cli.commands.init_templates import (
    _SAMPLE_CANDIDATE_MODEL,
    _SAMPLE_DATASOURCE,
    _SAMPLE_LLM,
    _SAMPLE_PRIORITISE_AGENT,
    _SAMPLE_TELEMETRY_CONFIG,
    _SAMPLE_TICKET_MODEL,
    _SAMPLE_TRIAGE_SPEC,
    _SAMPLE_TRIAGE_WORKFLOW,
    PRESETS,
)
from tuvl_insight.cli.select import Choice

console = Console()


_LLM_PROVIDERS = {
    #       env_key, prompt_label, default_val, secret, default_model
    **{name: preset[:5] for name, preset in PRESETS.items()},
    "other": ("LLM_API_BASE", "OpenAI-compatible endpoint URL", "", False, ""),
}


def init_cmd(
    name: str = typer.Argument(
        ".",
        help="Project directory name to scaffold. Defaults to current directory.",
    ),
    project_dir_opt: Path = typer.Option(
        None,
        "--project-dir",
        "-d",
        help="Alias for the NAME argument — path to scaffold the project into.",
        file_okay=False,
    ),
    sample: bool = typer.Option(
        False,
        "--sample",
        help="A 2.0 sample: ticket triage with a spec, task plan, tests and lock that pass the CI gate.",
        is_flag=True,
    ),
    multi_tenant: bool = typer.Option(
        False,
        "--multi-tenant",
        help=(
            "Enable multi-tenancy. Requires a PostgreSQL datasource to be "
            "configured at init time. The mode is locked into the database "
            "on first boot and cannot be changed without re-initialising."
        ),
        is_flag=True,
    ),
    no_ai_skills: bool = typer.Option(
        False,
        "--no-ai-skills",
        help="Skip scaffolding the .agents/ directory containing AI agent rules and skills.",
        is_flag=True,
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help="LLM preset: gemini, openai, anthropic or ollama (current model ids; the key is read "
        "from the environment, else left blank in .env).",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Accept every default without prompting (CI, coding agents)."
    ),
    decision_model: str | None = typer.Option(
        None,
        "--decision-model",
        help="Decision model for `decide` agents: laya (a self-hosted laya-serve container, "
        "added to `tuvl ship`) or none.",
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        help="Coding agent tuvl may run for you (see --list-agents), or `none`. Asked when omitted.",
    ),
    no_agent: bool = typer.Option(False, "--no-agent", help="Same as --agent none."),
    list_agents: bool = typer.Option(
        False, "--list-agents", help="Show the coding agents found on this machine and exit."
    ),
    initials: str | None = typer.Option(
        None,
        "--initials",
        help="Your initials (2-4 letters/digits); they stamp the ids of ideas and specs you create.",
    ),
    git: bool = typer.Option(
        True, "--git/--no-git", help="Run `git init` on the project when git is installed."
    ),
    branch: str = typer.Option("develop", "--branch", help="The initial git branch."),
    commit: bool = typer.Option(False, "--commit", help="Make an initial commit."),
    ci: str = typer.Option(
        "none",
        "--ci",
        help="Write the deterministic gate as CI: github (.github/workflows/tuvl.yml), gitlab "
        "(.gitlab-ci.yml) or none.",
    ),
    dev_telemetry: str | None = typer.Option(
        None,
        "--dev-telemetry",
        help="https address of your team's telemetry collector (optional; "
        "tuvl set dev-telemetry changes it later).",
    ),
) -> None:
    """Scaffold a new tuvl project: its folders, a database connection, AI model presets and the coding-agent rules and skills; --sample adds a working example."""
    if list_agents:
        from tuvl_insight.cli.commands.agent import agents_table

        console.print(agents_table())
        raise typer.Exit()
    if sample:
        from tuvl.extras import require

        require("codegen")  # before anything is written
    project_dir = _resolve_target(name, project_dir_opt)
    initials, agent = _check_options(
        preset=preset,
        decision_model=decision_model,
        ci=ci,
        agent="none" if no_agent else agent,
        initials=initials,
        project_dir=project_dir,
    )

    if dev_telemetry:
        from tuvl_insight import devtelemetry

        try:
            devtelemetry.check_server(dev_telemetry)
        except devtelemetry.TelemetryError as exc:
            console.print(f"[red]--dev-telemetry: {exc}[/red]")
            raise typer.Exit(2) from exc

    console.print(Rule("[bold green]tuvl init[/bold green]"))
    console.print(
        "Answer a few questions to configure your project.\n"
        "[dim]Press Enter to accept the default shown in brackets.[/dim]\n"
    )
    if multi_tenant:
        console.print(
            Panel(
                "[bold yellow]Multi-tenancy enabled.[/bold yellow]\n\n"
                "The deployment mode will be locked into the database on first boot.\n"
                "A PostgreSQL datasource [bold]must[/bold] be configured now — "
                "it cannot be added later without re-initialising.",
                border_style="yellow",
            )
        )

    answers = Answers(
        pg=_ask_postgres(multi_tenant=multi_tenant, yes=yes),
        llm=_ask_llm(preset, yes=yes),
        use_laya=_ask_decision_model(decision_model, yes=yes),
        **_ask_identity(initials, agent, yes=yes),
    )
    telemetry_url = dev_telemetry or _ask_dev_telemetry(yes=yes)
    summary = _scaffold(
        project_dir,
        answers,
        sample=sample,
        multi_tenant=multi_tenant,
        no_ai_skills=no_ai_skills,
        git=git,
        branch=branch,
        commit=commit,
        ci=ci,
    )
    if telemetry_url:
        from tuvl_insight import devtelemetry

        devtelemetry.save(
            project_dir,
            devtelemetry.Config(enabled=True, server=devtelemetry.check_server(telemetry_url)),
        )
    console.print()
    console.print(
        Panel(
            _summary_text(project_dir, answers, summary, multi_tenant, no_ai_skills),
            title="[green]✔  tuvl init complete[/green]",
            border_style="green",
        )
    )
    if telemetry_url:
        console.print(
            f"[dim]Developer actions go to {telemetry_url}. Register once with "
            "[/dim][bold]tuvl register developer[/bold][dim] (ask your team for the join code); "
            "your token stays in your own config folder, never in the project.[/dim]"
        )


# ── what the command was asked for ────────────────────────────────────────────


def _resolve_target(name: str, project_dir_opt: Path | None) -> Path:
    """The project directory.

    ``tuvl init trade`` → ``./trade/``; ``tuvl init trade -d /x`` → ``/x/trade/``;
    ``tuvl init -d /x/trade`` → ``/x/trade/``; ``tuvl init .`` initialises in place.
    """
    if project_dir_opt is None:
        return Path(name).resolve()
    if name == ".":
        return Path(project_dir_opt).resolve()
    return (Path(project_dir_opt) / name).resolve()


def _refuse(message: str, code: int = 2) -> typer.Exit:
    console.print(message)
    return typer.Exit(code)


def _check_options(
    *,
    preset: str | None,
    decision_model: str | None,
    ci: str,
    agent: str | None,
    initials: str | None,
    project_dir: Path,
) -> tuple[str | None, str | None]:
    """Refuse what cannot work, before anything is asked or written; returns (initials, agent)."""
    from tuvl_insight import coding_agents

    if preset is not None and preset not in PRESETS:
        raise _refuse(f"[red]Unknown preset '{preset}'.[/red] Choose: {', '.join(PRESETS)}.")
    if decision_model not in (None, "laya", "none"):
        raise _refuse(f"[red]Unknown decision model '{decision_model}'.[/red] Choose: laya, none.")
    if ci not in ("none", "github", "gitlab"):
        raise _refuse(f"[red]Unknown --ci '{ci}'.[/red] Choose: github, gitlab, none.")
    if agent is not None and agent != "none" and coding_agents.get(agent) is None:
        names = ", ".join(a.id for a in coding_agents.AGENTS)
        raise _refuse(f"[red]Unknown agent '{agent}'.[/red] Choose: {names}, none.")
    if initials is not None:
        initials = initials.strip().upper()
        if not coding_agents.valid_initials(initials):
            raise _refuse(
                "[red]--initials must be 2-4 letters or digits, starting with a letter.[/red]"
            )
    if project_dir != Path(".").resolve() and project_dir.exists():
        raise _refuse(f"[red]Directory '{project_dir}' already exists.[/red]", 1)
    return initials, agent


# ── the questions ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PgAnswers:
    host: str
    port: str
    db: str
    user: str
    password: str


@dataclass(frozen=True)
class LlmAnswers:
    provider: str
    env_key: str
    value: str
    is_secret: bool
    model: str


@dataclass(frozen=True)
class Answers:
    pg: PgAnswers | None
    llm: LlmAnswers | None
    use_laya: bool
    initials: str
    agent: str


def _ask_postgres(*, multi_tenant: bool, yes: bool) -> PgAnswers | None:
    console.print("[bold]PostgreSQL[/bold]")
    # Multi-tenant mode needs a database, so it is not asked about.
    if not (multi_tenant or yes) and not typer.confirm(
        "  Do you want to configure postgres now?", default=True
    ):
        return None
    if yes:
        return PgAnswers("localhost", "5432", "tuvl", "postgres", "postgres")
    return PgAnswers(
        host=typer.prompt("  Host", default="localhost"),
        port=typer.prompt("  Port", default="5432"),
        db=typer.prompt("  Database", default="tuvl"),
        user=typer.prompt("  User", default="postgres"),
        password=typer.prompt("  Password", default="postgres", hide_input=True),
    )


def _ask_llm(preset: str | None, *, yes: bool) -> LlmAnswers | None:
    console.print()
    console.print("[bold]LLM provider[/bold]")
    if preset is not None:
        env_key, _label, default_value, is_secret, model = PRESETS[preset]
        console.print(f"  preset [bold]{preset}[/bold]: {model}")
        return LlmAnswers(preset, env_key, os.environ.get(env_key, default_value), is_secret, model)
    if yes or not typer.confirm("  Do you want to configure LLM provider now?", default=True):
        return None
    console.print("  " + " / ".join(f"[dim]{name}[/dim]" for name in _LLM_PROVIDERS))
    provider = typer.prompt("  Provider", default="ollama").strip().lower()
    while provider not in _LLM_PROVIDERS:
        console.print(
            f"  [red]Unknown provider '{provider}'.[/red] Choose: {', '.join(_LLM_PROVIDERS)}."
        )
        provider = typer.prompt("  Provider", default="ollama").strip().lower()
    env_key, label, default_value, is_secret, default_model = _LLM_PROVIDERS[provider]
    return LlmAnswers(
        provider,
        env_key,
        typer.prompt(f"  {label}", default=default_value, hide_input=is_secret),
        is_secret,
        typer.prompt("  Model", default=default_model),
    )


def _ask_decision_model(decision_model: str | None, *, yes: bool) -> bool:
    console.print()
    console.print("[bold]Decision model[/bold]")
    if decision_model is not None:
        return decision_model == "laya"
    if yes:
        return False
    return typer.confirm(
        "  Add a self-hosted Laya decision model for `decide` agents? (runs as a container)",
        default=False,
    )


def _ask_dev_telemetry(*, yes: bool) -> str | None:
    """Optional: a server that receives what developers do on this project (also: tuvl set dev-telemetry)."""
    if yes:
        return None
    from tuvl_insight import devtelemetry

    console.print("[bold]Developer telemetry[/bold] [dim](optional)[/dim]")
    while True:
        try:
            url = typer.prompt(
                "  Share developer actions through a team collector? Its https address, or Enter to skip",
                default="",
                show_default=False,
            ).strip()
        except (typer.Abort, EOFError):  # no more input: skip, as for Enter
            return None
        if not url:
            return None
        try:
            return devtelemetry.check_server(url)
        except devtelemetry.TelemetryError as exc:
            console.print(f"  [red]{exc}[/red]")


def _ask_identity(initials: str | None, agent: str | None, *, yes: bool) -> dict[str, str]:
    """Your initials (they stamp idea and spec ids) and the coding agent tuvl may run."""
    from tuvl_insight import coding_agents, gitutil

    console.print()
    console.print("[bold]You and your coding agent[/bold]")
    if initials is None:
        suggested = coding_agents.initials_from(gitutil.config_get(Path.cwd(), "user.name"))
        initials = suggested or "DEV"
        while not yes:
            initials = (
                typer.prompt("  Your initials (stamp your idea and spec ids)", default=initials)
                .strip()
                .upper()
            )
            if coding_agents.valid_initials(initials):
                break
            console.print("  [red]2-4 letters or digits, starting with a letter.[/red]")
    if agent is None:
        agent = _ask_agent(coding_agents.detect(), yes=yes)
    return {"initials": initials, "agent": agent}


def _ask_agent(detected: list[Any], *, yes: bool) -> str:
    from tuvl_insight import coding_agents

    if not detected:
        console.print(
            "  No coding agent found. You can add one later with [bold]tuvl use agent <agent>[/bold]."
        )
        return "none"
    if yes:
        return "none"
    from tuvl_insight.cli.commands.agent import agents_table

    console.print(agents_table())
    runnable = [
        d.spec.id for d in detected if coding_agents.runner.capability(d.spec.id) == "write"
    ]
    found = {d.spec.id for d in detected}
    hints = {
        "write": "drafts specs and builds tasks",
        "review": "reviews code only",
        "": "found, not driven by tuvl yet",
    }
    menu = [
        Choice(d.spec.id, d.spec.name, hints[coding_agents.runner.capability(d.spec.id)])
        for d in detected
    ] + [Choice("none", "none", "choose later with `tuvl use agent <agent>`")]
    picked = select.pick(
        "  Coding agent for tuvl to run", menu, runnable[0] if runnable else "none"
    )
    if picked is not None:
        return picked
    choice: str = (
        typer.prompt(
            "  Coding agent for tuvl to run (id or none)",
            default=runnable[0] if runnable else "none",
        )
        .strip()
        .lower()
    )
    while choice != "none" and choice not in found:
        console.print(f"  [red]'{choice}' was not found on this machine.[/red]")
        choice = typer.prompt("  Coding agent (id or none)", default="none").strip().lower()
    return choice


# ── the files ─────────────────────────────────────────────────────────────────

_SUBDIRS = (
    "models",
    "workflows",
    "datasources",
    "llms",
    "agents",
    "federation",
    "artifacts",
    "idea",
    "specs",
)


@dataclass
class Summary:
    """The lines of the closing panel that depend on what was written."""

    postgres: str = ""
    llm: str = ""
    laya: str = ""
    sample: str = ""
    ui: str = ""
    git: str = ""
    ci: str = ""


def _env_values(answers: Answers) -> dict[str, tuple[str, bool]]:
    values: dict[str, tuple[str, bool]] = {}
    if answers.pg:
        pg = answers.pg
        values |= {
            "POSTGRES_HOST": (pg.host, False),
            "POSTGRES_PORT": (pg.port, False),
            "POSTGRES_DB": (pg.db, False),
            "POSTGRES_USER": (pg.user, False),
            "POSTGRES_PASSWORD": (pg.password, True),
        }
    if answers.llm:
        values[answers.llm.env_key] = (answers.llm.value, answers.llm.is_secret)
    if answers.use_laya:
        values["LAYA_API_BASE"] = (laya.LAYA_LOCAL_URL, False)
        values["LAYA_API_KEY"] = ("", True)
    return values


def _scaffold(
    project_dir: Path,
    answers: Answers,
    *,
    sample: bool,
    multi_tenant: bool,
    no_ai_skills: bool,
    git: bool,
    branch: str,
    commit: bool,
    ci: str,
) -> Summary:
    """Write the project, in the order its parts depend on each other."""
    from tuvl.core.project_info import create_project, default_identity
    from tuvl_insight import audit, coding_agents

    summary = Summary()
    for subdir in _SUBDIRS:
        d = project_dir / subdir
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gitkeep").touch()

    env_values = _env_values(answers)
    _write_env(project_dir / ".env", env_values, redact=False)  # real values, never committed
    _write_env(project_dir / ".env.example", env_values, redact=True)  # safe to commit
    _ensure_gitignore(project_dir)

    tuvl_dir = project_dir / ".tuvl"
    tuvl_dir.mkdir(parents=True, exist_ok=True)
    _write_system_yaml(
        tuvl_dir / "system.yaml",
        deployment_mode="multi_tenant" if multi_tenant else "single_tenant",
    )
    who = default_identity(project_dir)
    project_name = project_dir.name
    create_project(project_dir, name=project_name, author=who["author"], email=who["email"])

    _write_models(project_dir, answers, summary)
    if sample:
        _write_sample_files(project_dir)
        summary.sample = _SAMPLE_SUMMARY
    if not no_ai_skills:
        _write_agent_skills(project_dir)

    _write_pyproject(
        project_dir / "pyproject.toml",
        name=project_name.lower().replace(" ", "-").replace("_", "-"),
    )
    _write_readme(project_dir / "README.md", name=project_name)
    if answers.use_laya:
        with (project_dir / "README.md").open("a") as fh:
            fh.write(_LAYA_README)
    if sample:
        _finish_sample(project_dir)

    summary.ui = _write_ui_specs(project_dir)
    coding_agents.write_settings(
        project_dir, coding_agents.Settings(answers.initials, answers.agent)
    )
    summary.ci = _write_ci(project_dir, ci)
    summary.git = _init_git(project_dir, enabled=git, branch=branch, commit=commit)
    audit.emit(
        project_dir,
        "init.completed",
        command="init",
        detail=project_name,
        refs={
            "agent": answers.agent,
            "git": bool(summary.git and "repository on branch" in summary.git),
            "sample": sample,
        },
    )
    return summary


def _write_models(project_dir: Path, answers: Answers, summary: Summary) -> None:
    """The datasource and AI-model YAMLs for what was configured."""
    if answers.pg:
        pg = answers.pg
        _write_pg_datasource(
            project_dir / "datasources" / "postgres.yaml",
            host=pg.host,
            port=pg.port,
            db=pg.db,
            user=pg.user,
            primary=True,
        )
        summary.postgres = "\n  [dim]datasources/postgres.yaml[/dim] ← PostgreSQL DataSource"
    if answers.llm:
        llm = answers.llm
        _write_agent_yaml(
            project_dir / "llms" / "default.yaml",
            name="default",
            provider=llm.provider,
            env_key=llm.env_key,
            llm_value=llm.value,
            model=llm.model,
        )
        summary.llm = "\n  [dim]llms/default.yaml[/dim] ← AgentModel for agents"
    if answers.use_laya:
        (project_dir / "llms" / "laya.yaml").write_text(laya.AGENT_MODEL_YAML)
        compose = project_dir / "compose.yaml"
        if not compose.exists():
            compose.write_text(laya.COMPOSE_YAML)
        summary.laya = (
            "\n  [dim]llms/laya.yaml[/dim] ← Laya decision model (decide.model.ref: laya)"
            "\n  [dim]compose.yaml[/dim]   ← local Laya: [bold]docker compose up -d laya[/bold]"
        )


_SAMPLE_SUMMARY = (
    "\n\n  [bold]Sample files[/bold] (--sample):\n"
    "  [dim]models/candidate.yaml[/dim]          ← ModelDefinition (every field type)\n"
    "  [dim]models/ticket.yaml[/dim]             ← ModelDefinition (read-only CRUD opt-in)\n"
    "  [dim]workflows/triage_ticket.yaml[/dim]   ← 2.0 workflow: llm, code, human, tool agents\n"
    "  [dim]agents/prioritise_ticket.py[/dim]    ← @agent code agent\n"
    "  [dim]specs/triage-ticket.md[/dim]         ← the spec (+ .tasks.yaml TaskPlan)\n"
    "  [dim]tests/triage_ticket/[/dim]           ← Tests generated from its examples\n"
    "  [dim]tuvl.lock[/dim]                      ← pins (tuvl generate lock)\n"
    "  [dim]datasources/sample.yaml[/dim]        ← DataSource (PostgreSQL)\n"
    "  [dim]llms/default.yaml[/dim]              ← AgentModel (LLM config)\n"
    "  [dim].tuvl/telemetry.yaml[/dim]           ← OTel telemetry config"
)


def _summary_text(
    project_dir: Path,
    answers: Answers,
    summary: Summary,
    multi_tenant: bool,
    no_ai_skills: bool,
) -> str:
    skills = (
        "\n  [dim].agents/[/dim]        ← Open-standard agent configuration directory\n"
        "  [dim]AGENTS.md[/dim]         ← Instructions and rules for AI agents"
        if not no_ai_skills
        else ""
    )
    mode = (
        "\n  [dim].tuvl/system.yaml[/dim]  ← deployment_mode: "
        f"[bold]{'multi_tenant' if multi_tenant else 'single_tenant'}[/bold]"
    )
    return (
        f"Project scaffolded at [bold]{project_dir}[/bold]\n\n"
        "  [dim]pyproject.toml[/dim]  ← add extra deps here with [bold]uv add <pkg>[/bold]\n"
        "  [dim]models/[/dim]        ← ModelDefinition YAMLs\n"
        "  [dim]workflows/[/dim]     ← Workflow YAMLs\n"
        "  [dim]datasources/[/dim]   ← DataSource / RedisConfig YAMLs"
        f"{summary.postgres}\n"
        "  [dim]llms/[/dim]          ← AgentModel YAMLs (LLM presets)"
        f"{summary.llm}{summary.laya}\n"
        "  [dim]agents/[/dim]        ← Python code agents (@agent)\n"
        "  [dim]federation/[/dim]    ← FederationProvider YAMLs\n"
        "  [dim].env[/dim]           ← secrets (git-ignored)\n"
        "  [dim].env.example[/dim]   ← safe-to-commit template\n"
        "  [dim].gitignore[/dim]     ← .env already listed\n"
        f"  [dim].tuvl/agent.yaml[/dim] ← you: [bold]{answers.initials}[/bold], "
        f"coding agent: [bold]{answers.agent}[/bold]"
        f"{summary.ui}{summary.git}{summary.ci}{skills}{mode}{summary.sample}\n\n"
        "  tuvl auto-discovers all YAML files by [bold]kind:[/bold] — no config.yaml required\n\n"
        f"Next: [bold]cd {project_dir.name} && uv sync && tuvl check system && tuvl dev --project-dir .[/bold]"
    )


_LAYA_README = f"""
## Decision model: Laya

`llms/laya.yaml` is a self-hosted [Laya](https://pypi.org/project/laya/) decision model. Point a
`decide` agent at it with `decide.model.ref: laya`; it answers with calibrated probabilities.

- Local: `docker compose up -d laya` (image `{laya.LAYA_IMAGE_REPOSITORY}:{laya.LAYA_IMAGE_TAG_EN}`,
  English only, on `{laya.LAYA_LOCAL_URL}`). For other languages set `model: auto` in
  `llms/laya.yaml` and use the `{laya.LAYA_IMAGE_TAG}` image.
- Production: `tuvl ship` adds a Laya Deployment and Service to the Helm chart and sets
  `LAYA_API_BASE` for the engine (`--without-laya` leaves it out). It picks the English-only image
  (about 2 GiB of memory) when the model is `english`, and the full image (about 4 GiB) otherwise.
"""


def _write_sample_files(project_dir: Path) -> None:
    """Write a small 2.0 sample: a ticket-triage workflow and its pieces."""
    (project_dir / "models" / "candidate.yaml").write_text(_SAMPLE_CANDIDATE_MODEL)
    (project_dir / "models" / "ticket.yaml").write_text(_SAMPLE_TICKET_MODEL)
    (project_dir / "workflows" / "triage_ticket.yaml").write_text(_SAMPLE_TRIAGE_WORKFLOW)
    (project_dir / "agents" / "prioritise_ticket.py").write_text(_SAMPLE_PRIORITISE_AGENT)
    (project_dir / "datasources" / "sample.yaml").write_text(_SAMPLE_DATASOURCE)
    # Write llms/default.yaml only if not already created by the LLM-provider prompt
    llm_path = project_dir / "llms" / "default.yaml"
    if not llm_path.exists():
        llm_path.write_text(_SAMPLE_LLM)

    # A spec, its task list (as the coding agent writes it) and the implementation.
    (project_dir / "specs").mkdir(exist_ok=True)
    (project_dir / "specs" / "triage-ticket.md").write_text(_SAMPLE_TRIAGE_SPEC)
    (project_dir / "specs" / "triage-ticket.tasks.yaml").write_text(_sample_task_plan())

    # .tuvl/telemetry.yaml — default OTel config readable by the Dev UI.
    # system.yaml is written by init_cmd before this runs; not overwritten here.
    tuvl_dir = project_dir / ".tuvl"
    tuvl_dir.mkdir(exist_ok=True)
    (tuvl_dir / "telemetry.yaml").write_text(_SAMPLE_TELEMETRY_CONFIG)


def _sample_task_plan() -> str:
    import yaml

    from tuvl_insight.spec import parse_text

    spec = parse_text(_SAMPLE_TRIAGE_SPEC, path="specs/triage-ticket.md")

    def ref(anchor: str) -> dict[str, str]:
        section = spec.section(anchor)
        assert section is not None
        return {"anchor": anchor, "hash": section.hash}

    flow, wf = ref("flow-triage-ticket"), "triage_ticket"
    steps = [
        ("classify", "Classify tickets (llm)"),
        ("prioritise", "Prioritise tickets (code)"),
        ("review", "Lead review of urgent tickets (human)"),
        ("store", "Store the ticket (tool db)"),
    ]
    tasks: list[dict[str, Any]] = [
        {
            "id": "T1",
            "title": "Ticket model",
            "type": "model",
            "spec_ref": ref("data"),
            "target": {"model": "Ticket"},
        },
        *(
            {
                "id": f"T{n}",
                "title": title,
                "type": "agent",
                "spec_ref": flow,
                "target": {"workflow": wf, "agent": agent},
                "depends_on": ["T1"] if n == 2 else [f"T{n - 1}"],
            }
            for n, (agent, title) in enumerate(steps, start=2)
        ),
        *(
            {
                "id": f"T{n}",
                "title": f"Example: {example.name}",
                "type": "test",
                "spec_ref": ref("acceptance"),
                "target": {"workflow": wf, "example": example.name},
                "depends_on": ["T5"],
            }
            for n, example in enumerate(spec.examples, start=6)
        ),
    ]
    doc = {
        "kind": "TaskPlan",
        "version": "tuvl/v2",
        "metadata": {"name": spec.name, "spec": spec.path, "spec_hash": spec.hash},
        "spec": {"tasks": tasks},
    }
    return str(yaml.safe_dump(doc, sort_keys=False))


def _finish_sample(project_dir: Path) -> None:
    """Run codegen and lock so the sample passes the CI gate as created."""
    from dotenv import load_dotenv

    from tuvl.contract.registry import TESTS, WORKFLOWS
    from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY
    from tuvl.core.config_loader import load_all_configs
    from tuvl.core.models.schemas import SCHEMA_REGISTRY
    from tuvl_insight import codegen, lock
    from tuvl_insight.spec import load_specs

    use_project(str(project_dir))
    load_dotenv(project_dir / ".env", override=False)  # datasources reference its variables
    load_all_configs(project_dir)
    generated = codegen.plan(
        project_dir,
        dict(WORKFLOWS),
        SCHEMA_REGISTRY,
        load_specs(project_dir),
        artifacts=set(ARTIFACT_REGISTRY),
    )
    codegen.apply(project_dir, generated.changes)
    pinned = lock.build(lock.inventory(dict(WORKFLOWS), TESTS.values()))  # no MCP in the sample
    (project_dir / lock.LOCK_FILE).write_text(lock.render(pinned))


def _write_env(path: Path, values: dict[str, tuple[str, bool]], *, redact: bool) -> None:
    lines = [
        "# Generated by `tuvl init`.",
        (
            "# DO NOT commit .env — it contains real secrets."
            if not redact
            else "# Safe to commit. Real secrets are in .env (git-ignored)."
        ),
        "",
    ]
    pg_keys = [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ]
    pg_present = any(k in values for k in pg_keys)
    if pg_present:
        lines.append("# PostgreSQL")
        for k in pg_keys:
            if k in values:
                v, secret = values[k]
                lines.append(f"{k}={'' if (redact and secret) else v}")
        lines.append("")

    llm_keys = {k: v for k, v in values.items() if k not in pg_keys}
    if llm_keys:
        lines.append("# LLM")
        for k, (v, secret) in llm_keys.items():
            lines.append(f"{k}={'' if (redact and secret) else v}")

    path.write_text("\n".join(lines) + "\n")
    # Real .env files contain plaintext secrets (DB password, LLM API keys).
    # Ship them owner-only so shared dev hosts don't leak via ``cat`` from
    # another account or via tooling that walks the project tree.
    if not redact:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def _write_system_yaml(path: Path, *, deployment_mode: str) -> None:
    """Write .tuvl/system.yaml — the project-level system configuration.

    ``deployment_mode`` is read by the bootstrap pre-flight check at every
    boot.  The database is the final source of truth; this file only provides
    the *requested* mode for the greenfield (first-ever) boot.

    Valid values: ``single_tenant`` | ``multi_tenant``.
    """
    content = (
        "# tuvl system configuration\n"
        "# Generated by `tuvl init`. Do not edit deployment_mode after first boot\n"
        "# — the database lock takes precedence and a mismatch will crash startup.\n"
        "#\n"
        "# deployment_mode:\n"
        "#   single_tenant  — all data shared in a single schema (default)\n"
        "#   multi_tenant   — tenant_id column injected into every model;\n"
        "#                    Row-Level Security policies enforced per request\n"
        "kind: SystemConfig\n"
        "version: v1\n"
        "metadata:\n"
        "  name: system\n"
        "spec:\n"
        f"  deployment_mode: {deployment_mode}\n"
        "  # api:\n"
        "  #   # Set false to not mount the /models/* CRUD API — only workflow\n"
        "  #   # routes are exposed. Restart required; the env var\n"
        "  #   # TUVL_EXPOSE_MODEL_CRUD overrides this value when set.\n"
        "  #   expose_model_crud: true\n"
        "  #   # Browser origins allowed to call the API (scheme://host[:port], no trailing\n"
        "  #   # slash). None by default; TUVL_CORS_ORIGINS overrides.\n"
        "  #   cors_origins: []\n"
    )
    path.write_text(content)


def _write_pg_datasource(
    path: Path,
    *,
    host: str,
    port: str,
    db: str,
    user: str,
    primary: bool = False,
) -> None:
    """Write the DataSource YAML with ``${VAR}`` env references, never literals.

    The password is env-only (no inline default) so the committable YAML and
    the ship-built image carry no secret — the real value lives in ``.env``
    locally and in the Helm ``existingSecret`` in production. Non-secret
    connection fields keep the values entered at init as inline defaults so
    the project boots even without a ``.env``.
    """
    primary_line = "  primary: true\n" if primary else ""
    content = (
        "kind: DataSource\n"
        "version: v1\n"
        "enabled: true\n"
        "metadata:\n"
        "  name: main_postgres\n"
        f"{primary_line}"
        "  description: Primary PostgreSQL database\n"
        "spec:\n"
        "  type: postgresql\n"
        "  driver: asyncpg\n"
        "  connection:\n"
        f"    host: ${{POSTGRES_HOST:{host}}}\n"
        f"    port: ${{POSTGRES_PORT:{port}}}\n"
        f"    database: ${{POSTGRES_DB:{db}}}\n"
        f"    username: ${{POSTGRES_USER:{user}}}\n"
        "    password: ${POSTGRES_PASSWORD}\n"
        "  pooling:\n"
        "    min_size: 5\n"
        "    max_size: 20\n"
    )
    path.write_text(content)


def _write_agent_yaml(
    path: Path,
    *,
    name: str = "default",
    provider: str,
    env_key: str,
    llm_value: str,
    model: str,
) -> None:
    spec_lines = [f"  model: {model}"]
    if provider in ("ollama", "other"):
        spec_lines.append(f"  api_base: {llm_value}")
    else:  # openai, anthropic — key goes in .env, reference it via env var
        spec_lines.append(f"  api_key: ${{{env_key}}}")
    spec_lines.append("  temperature: 0.7")
    lines = [
        "kind: AgentModel",
        "version: v1",
        "enabled: true",
        "metadata:",
        f"  name: {name}",
        "spec:",
        *spec_lines,
    ]
    path.write_text("\n".join(lines) + "\n")


def _write_ui_specs(project_dir: Path) -> str:
    """``ui/specs/``: the index, plus a file for each workflow that is already implemented (the sample)."""
    from tuvl_insight import uispec

    try:
        uispec.generate(project_dir, run_tests=False)
    except Exception:
        (project_dir / uispec.SPECS_DIR).mkdir(parents=True, exist_ok=True)
        return ""
    return "\n  [dim]ui/specs/[/dim]       ← how to build the UI on this backend (`tuvl generate ui-spec`)"


def _write_ci(project_dir: Path, kind: str) -> str:
    from tuvl_insight.cli.commands import init_templates as tpl

    if kind == "github":
        path, text = project_dir / ".github" / "workflows" / "tuvl.yml", tpl.github_workflow()
    elif kind == "gitlab":
        path, text = project_dir / ".gitlab-ci.yml", tpl.gitlab_pipeline()
    else:
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return f"\n  [dim]{path.relative_to(project_dir)}[/dim] ← the deterministic gate as CI"


def _init_git(project_dir: Path, *, enabled: bool, branch: str, commit: bool) -> str:
    """``git init`` the new project; the returned text is for the summary panel."""
    from tuvl_insight import gitutil

    if not enabled:
        return ""
    if not gitutil.available():
        return "\n  [dim]git[/dim] not installed: skipped (https://git-scm.com/downloads)"
    if gitutil.inside_work_tree(project_dir):
        return "\n  [dim]git[/dim] already inside a repository: left alone"
    if not gitutil.init_repo(project_dir, branch):
        return "\n  [yellow]git init failed[/yellow]: run it yourself"
    line = f"\n  [dim]git[/dim] repository on branch [bold]{branch}[/bold]"
    if commit:
        if not (
            gitutil.config_get(project_dir, "user.name")
            and gitutil.config_get(project_dir, "user.email")
        ):
            return line + " (no commit: set git user.name and user.email first)"
        if gitutil.commit_all(project_dir, "Initial tuvl project"):
            line += ", initial commit made"
        else:
            line += " (the initial commit failed)"
    return line


# machine-local files under .tuvl/; the rest of the directory (project.yaml, system.yaml, …) is committed
_TUVL_LOCAL = (
    ".tuvl/agent.yaml",
    ".tuvl/.dev-session",
    ".tuvl/audit/",
    ".tuvl/trace/",
    ".tuvl/live/",
    ".tuvl/worktrees/",
)


def _ensure_gitignore(project_dir: Path) -> None:
    gi = project_dir / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    entries_needed = [
        ".env",
        "__pycache__/",
        "*.pyc",
        ".venv/",
        ".python-version",
        *_TUVL_LOCAL,
    ]
    missing = [e for e in entries_needed if e not in existing.splitlines()]
    if missing:
        with gi.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("# tuvl\n")
            f.write("\n".join(missing) + "\n")


def _write_pyproject(path: Path, *, name: str) -> None:
    """Write a pyproject.toml with full tuvl metadata and project URLs."""
    if path.exists():
        return
    try:
        tuvl_version = importlib.metadata.version("tuvl")
        tuvl_dep = f'"tuvl>={tuvl_version}"'
        tuvl_dev_dep = f'"tuvl[standard]>={tuvl_version}"'
        prerelease = re.search(r"(a|b|rc|\.dev)\d", tuvl_version) is not None
    except importlib.metadata.PackageNotFoundError:
        tuvl_dep = '"tuvl"'
        tuvl_dev_dep = '"tuvl[standard]>=2.0.3"'
        prerelease = True
    content = (
        "# ── tuvl project ─────────────────────────────────────────────────────────────\n"
        "#\n"
        "# Built with tuvl — YAML-driven AI workflow orchestration for Python backends.\n"
        "#\n"
        "#   Website:       https://tuvl.io\n"
        "#   Documentation: https://tuvl.dev\n"
        "#   PyPI:          https://pypi.org/project/tuvl/\n"
        "#   GitHub:        https://github.com/tuvl-io/\n"
        "#\n"
        "# Add extra dependencies with: uv add <package>\n"
        "# ─────────────────────────────────────────────────────────────────────────────\n"
        "[project]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        f'description = "A tuvl project — see https://tuvl.io"\n'
        'readme = "README.md"\n'
        'requires-python = ">=3.13,<3.15"\n'
        "dependencies = [\n"
        f"    {tuvl_dep},\n"
        "]\n"
        "\n"
        "[project.urls]\n"
        '"Homepage"      = "https://tuvl.io"\n'
        '"Documentation" = "https://tuvl.dev"\n'
        '"tuvl on PyPI"  = "https://pypi.org/project/tuvl/"\n'
        '"Source"        = "https://github.com/tuvl-io/"\n'
        "\n"
        "[dependency-groups]\n"
        "# Development: uv injects the UI and tools automatically for local work\n"
        "dev = [\n"
        f"    {tuvl_dev_dep},\n"
        "]\n"
        "\n"
        "[tool.uv]\n"
        + ('prerelease = "allow"  # tuvl 2.0 is still in beta\n' if prerelease else "")
        + "# uv manages the virtual environment.\n"
        "# Run: uv sync          — install / refresh the venv\n"
        "# Run: uv add <pkg>     — add a new runtime dependency\n"
        "# Run: uv add --dev <pkg> — add a dev-only dependency\n"
    )
    path.write_text(content)


def _write_readme(path: Path, *, name: str) -> None:
    """Write a README.md with quick-start and links to tuvl.io / tuvl.dev."""
    if path.exists():
        return
    content = (
        f"# {name}\n"
        "\n"
        "> Built with [tuvl](https://tuvl.io) — compose AI-powered backend workflows\n"
        "> as typed YAML workflows of agents, run by a durable, journaled runtime.\n"
        "\n"
        "## Quick Start\n"
        "\n"
        "```bash\n"
        "# 1. Install dependencies\n"
        "uv sync\n"
        "\n"
        "# 2. Copy the secrets template and fill in your values\n"
        "cp .env.example .env\n"
        "\n"
        "# 3. Start the development server\n"
        "tuvl dev --project-dir .\n"
        "```\n"
        "\n"
        "The engine starts at **http://localhost:8000** and the\n"
        "[Tuvl Insight](https://tuvl.io) developer portal is available at\n"
        "**http://localhost:8000/insight**.\n"
        "\n"
        "## Project structure\n"
        "\n"
        "```\n"
        f"{name}/\n"
        "├── pyproject.toml       # Python project & dependencies\n"
        "├── .env                 # secrets (git-ignored)\n"
        "├── .env.example         # safe-to-commit template\n"
        "├── .gitignore\n"
        "├── models/              # ModelDefinition YAMLs — database schema\n"
        "├── workflows/           # Workflow YAMLs — HTTP-triggered pipelines\n"
        "├── datasources/         # DataSource / RedisConfig YAMLs\n"
        "├── llms/                # AgentModel YAMLs — LLM model configs\n"
        "├── federation/          # FederationProvider YAMLs — OAuth providers\n"
        "└── agents/              # Python code agents (@agent decorator)\n"
        "```\n"
        "\n"
        "> **`--sample` flag** adds a ticket-triage workflow using the `llm`, `code`, `human`\n"
        "> and `tool` engines, with its model and code agent.\n"
        "\n"
        "## Common tasks\n"
        "\n"
        "| Task | Command |\n"
        "|------|---------|\n"
        "| Start dev server | `tuvl dev --project-dir .` |\n"
        "| Validate all files | `tuvl lint project --project-dir .` |\n"
        "| Package for production | `tuvl ship` — container image + Helm chart |\n"
        "| Add a Python dep | `uv add <package>` |\n"
        '| Add a code agent | Create `agents/<name>.py`, use `@agent("<name>")` |\n'
        "| Add a workflow | Create `workflows/<name>.yaml` with `kind: Workflow` |\n"
        "\n"
        "## Resources\n"
        "\n"
        "| | |\n"
        "|--|--|\n"
        "| **Website** | <https://tuvl.io> |\n"
        "| **Documentation** | <https://tuvl.dev> |\n"
        "| **Getting started** | [tuvl.dev/getting-started](https://tuvl.dev/getting-started/quickstart/) |\n"
        "| **Concepts** | [tuvl.dev/concepts](https://tuvl.dev/concepts/architecture/) |\n"
        "| **CLI reference** | [tuvl.dev/cli](https://tuvl.dev/cli/commands/) |\n"
        "| **API reference** | [tuvl.dev/api](https://tuvl.dev/api/endpoints/) |\n"
        "| **PyPI** | [pypi.org/project/tuvl](https://pypi.org/project/tuvl/) |\n"
        "| **GitHub** | [github.com/tuvl-io](https://github.com/tuvl-io/) |\n"
        "\n"
        "---\n"
        "\n"
        "*Generated by `tuvl init`. Edit freely.*\n"
    )
    path.write_text(content)


def _write_agent_skills(project_dir: Path) -> None:
    """AGENTS.md and the coding-agent skills (.agents/skills, .claude/skills, CLAUDE.md)."""
    from tuvl_insight import skills

    skills.install(project_dir)
