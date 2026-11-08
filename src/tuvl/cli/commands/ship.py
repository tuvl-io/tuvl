"""``tuvl ship`` — package a tuvl project for production.

Validates the project, writes a production Dockerfile + .dockerignore and a
Helm chart under ``deploy/``, then builds (and optionally pushes) the image.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from tuvl.cli import laya
from tuvl.cli.commands import ship_deps
from tuvl.cli.commands import ship_templates as tpl
from tuvl.cli.commands.validate import _print_report, run_validation
from tuvl.extras import EXTRAS

if TYPE_CHECKING:
    from tuvl import shiparchive

console = Console()


def _local_storage_mounts(project_dir: Path) -> list[dict[str, str]]:
    """One mount per distinct ``provider: local`` store root: container path and PVC subPath."""
    import posixpath

    import yaml

    from tuvl.storage import ConfigError, parse_store

    mounts: dict[str, str] = {}
    for path in sorted((project_dir / "storage").glob("*.y*ml")):
        try:
            data = yaml.safe_load(path.read_text())
            name, spec = parse_store(data if isinstance(data, dict) else {})
        except (OSError, yaml.YAMLError, ConfigError):
            continue
        if spec.provider != "local":
            continue
        if "${" in spec.root:
            console.print(
                f"  [yellow]![/yellow] store '{name}': root {spec.root!r} uses an environment "
                "variable — mount its volume by hand"
            )
            continue
        target = (
            posixpath.normpath(spec.root)
            if spec.root.startswith("/")
            else posixpath.normpath(posixpath.join("/app", spec.root))
        )
        mounts[target] = target.strip("/").replace("/", "-") or "root"
    return [{"path": p, "subPath": s} for p, s in sorted(mounts.items())]


def _project_meta(project_dir: Path) -> tuple[str, str]:
    """(slug, version) from pyproject.toml, falling back to the directory name.

    A missing pyproject.toml is fine (fallback silently). A *malformed* one is
    surfaced loudly — silently shipping ``<dirname>:0.1.0`` hides a real error.
    """
    name, version = project_dir.name, "0.1.0"
    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text())
            project = data.get("project") or {}
            name = project.get("name") or name
            version = project.get("version") or version
        except (OSError, tomllib.TOMLDecodeError) as exc:
            console.print(
                f"[red bold]Ship aborted — could not parse pyproject.toml: {exc}[/red bold]\n"
                "Fix it or remove it (ship falls back to the directory name)."
            )
            raise typer.Exit(1) from exc
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tuvl-app"
    return slug, version


_APP_VERSION_RE = re.compile(r"^appVersion:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)


def _warn_chart_version_skew(chart_yaml: Path, version: str) -> None:
    """Warn when a kept Chart.yaml's appVersion no longer matches the build
    version — the deployed image tag (empty → .Chart.AppVersion) would point
    at an image this ship did not build."""
    if not chart_yaml.exists():
        return
    m = _APP_VERSION_RE.search(chart_yaml.read_text())
    if m and m.group(1).strip() != version:
        console.print(
            f"  [yellow]![/yellow] kept Chart.yaml pins appVersion "
            f"[bold]{m.group(1).strip()}[/bold] but this build is [bold]{version}[/bold] — "
            "the deployed image tag will not match. Re-run with --force or bump appVersion."
        )


# Validation warnings that block a ship: a `pending` agent is a contract, not an implementation.
_SHIP_ERRORS = frozenset({"V010", "V029"})

# Development material that must never reach the runtime image (V027).
_DEV_NAMES = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        ".mcp.json",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    }
)
_DEV_DIRS = (
    "specs/",
    "tests/",
    ".agents/",
    ".claude/",
    ".git/",
    ".tuvl/audit/",
    ".tuvl/trace/",
    ".tuvl/live/",
    ".tuvl/worktrees/",
)
_DEV_FILES = (".tuvl/.dev-session", ".tuvl/agent.yaml", ".tuvl/dev-telemetry.yaml")
_DEV_KIND = re.compile(r"^kind:\s*(Test|TaskPlan)\s*$", re.MULTILINE)
_WALK_SKIP = frozenset({".git", ".venv", "node_modules", "__pycache__", "deploy"})


def _ignore_rules(dockerignore: Path) -> list[tuple[bool, str]]:
    """(re-include?, pattern) per .dockerignore line, in order."""
    rules = []
    for raw in dockerignore.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            negate = line.startswith("!")
            rules.append((negate, line.lstrip("!").rstrip("/")))
    return rules


def _matches(rel: str, pattern: str) -> bool:
    parts = rel.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    if pattern.startswith("**/"):
        tail = pattern[3:]
        suffixes = ["/".join(parts[i:]) for i in range(len(parts))]
        return any(fnmatch.fnmatchcase(x, tail) for x in suffixes + parts)
    return any(fnmatch.fnmatchcase(x, pattern) for x in prefixes)


def _image_files(project_dir: Path, rules: list[tuple[bool, str]]) -> list[str]:
    """Project files the build context sends, by Docker's last-match-wins rule."""
    files = []
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _WALK_SKIP]
        for name in names:
            rel = Path(root, name).relative_to(project_dir).as_posix()
            included = True
            for negate, pattern in rules:
                if _matches(rel, pattern):
                    included = negate
            if included:
                files.append(rel)
    return sorted(files)


def _check_image_context(project_dir: Path) -> list[str]:
    """V027: what the image would contain, and any development material in it."""
    dockerignore = project_dir / ".dockerignore"
    rules = _ignore_rules(dockerignore) if dockerignore.exists() else []
    problems = []
    if not rules or rules[0] != (False, "*"):
        problems.append(
            ".dockerignore is not an allowlist (its first rule must be `*`) — development files "
            "would enter the image; run `tuvl ship --force` to regenerate it"
        )
    files = _image_files(project_dir, rules)
    for rel in files:
        dev = (
            Path(rel).name in _DEV_NAMES
            or rel.startswith(_DEV_DIRS)
            or rel in _DEV_FILES
            or rel.endswith(".tasks.yaml")
            or Path(rel).name == ".env"
            or Path(rel).name.startswith(".env.")
        )
        if not dev and rel.endswith((".yaml", ".yml")):
            try:
                dev = bool(_DEV_KIND.search((project_dir / rel).read_text(errors="ignore")))
            except OSError:
                dev = False
        if dev:
            problems.append(f"development file in the image: {rel}")
    declared = list((project_dir / "storage").glob("*.y*ml"))
    if declared and not any(rel.startswith("storage/") for rel in files):
        console.print(
            "  [yellow]![/yellow] storage/ is not in the image, so its stores would not load — add "
            "[bold]!storage/[/bold] to .dockerignore or run `tuvl ship --force`"
        )
    top: dict[str, int] = {}
    for rel in files:
        key = rel.split("/", 1)[0] + ("/" if "/" in rel else "")
        top[key] = top.get(key, 0) + 1
    console.print(
        f"  image contents: {len(files)} file(s) — "
        + ", ".join(f"{k} {v}" if k.endswith("/") else k for k, v in sorted(top.items()))
    )
    return problems


def _write(
    path: Path, content: str, *, force: bool, project_dir: Path, written: list[str], kept: list[str]
) -> None:
    rel = str(path.relative_to(project_dir))
    if path.exists() and not force:
        kept.append(rel)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    written.append(rel)


def _check_lock(project_dir: Path) -> str:
    """V024: the lock must exist and match the project. Returns the pinned tuvl version."""
    from tuvl import lock
    from tuvl.cli.commands.lock import fresh_lock

    try:
        fresh, current = fresh_lock(project_dir, live_mcp=False)
    except Exception as exc:
        console.print(f"[red]Ship aborted — tuvl generate lock: {exc}[/red]")
        raise typer.Exit(1) from exc
    if current is None:
        console.print(
            f"[red bold]Ship aborted — [V024] {lock.LOCK_FILE} is missing. Run `tuvl generate lock`.[/red bold]"
        )
        raise typer.Exit(1)
    changed = lock.differences(current, fresh, live_mcp=False)
    if changed:
        for line in changed:
            console.print(f"  [yellow]~[/yellow] {line}")
        console.print(
            f"[red bold]Ship aborted — [V024] {lock.LOCK_FILE} is out of date. Run `tuvl generate lock`.[/red bold]"
        )
        raise typer.Exit(1)
    console.print(f"[green]✓ {lock.LOCK_FILE} is current[/green] (tuvl {current['tuvl']})")
    return str(current["tuvl"])


def _resolve_extras(project_dir: Path, forced: tuple[str, ...]) -> list[str]:
    """The tuvl extras the image installs; aborts on a package the project does not declare."""
    unknown = [e for e in forced if e not in EXTRAS or not EXTRAS[e].shipped]
    if unknown:
        shippable = ", ".join(n for n, e in EXTRAS.items() if e.shipped)
        console.print(f"[red]--extra {', '.join(unknown)}: choose from {shippable}.[/red]")
        raise typer.Exit(1)
    needs = ship_deps.detect(project_dir, forced)
    if needs.undeclared:
        for rel, module in needs.undeclared:
            console.print(
                f"  [red]✗[/red] {rel} imports [bold]{module}[/bold], which pyproject.toml does not declare"
            )
        console.print(
            "[red bold]Ship aborted — the image would fail at run time. "
            "Add the package with `uv add <package>`.[/red bold]"
        )
        raise typer.Exit(1)
    for rel, module in needs.unresolved:
        console.print(
            f"  [yellow]![/yellow] {rel} imports {module}, which is not installed here, so "
            "`tuvl ship` cannot check that it is declared"
        )
    if needs.unused:
        console.print(
            f"  [dim]declared but not imported by any agent: {', '.join(needs.unused)} "
            "(kept; remove from pyproject.toml to slim the image)[/dim]"
        )
    names = needs.names
    left_out = [n for n, e in EXTRAS.items() if e.shipped and n not in names]
    if names:
        console.print(f"[green]✓ tuvl extras:[/green] {', '.join(names)}")
        for name in names:
            console.print(f"    [dim]{name}: {needs.extras[name][0]}[/dim]")
    else:
        console.print("[green]✓ tuvl extras:[/green] none — the base install is enough")
    if left_out:
        console.print(f"  [dim]not installed: {', '.join(left_out)}[/dim]")
    return names


def _stale_dockerfile(dockerfile: Path, extras: list[str]) -> str | None:
    """Why a kept deploy/Dockerfile would build an image missing an extra the project needs."""
    recorded = ship_deps.recorded_extras(dockerfile)
    if recorded is None:
        if not extras:
            return None
        return (
            "the kept Dockerfile predates extras and installs the base tuvl only, but the "
            f"project needs {', '.join(extras)} — run `tuvl ship --force`"
        )
    missing = sorted(set(extras) - recorded)
    if missing:
        return (
            f"the kept Dockerfile does not install tuvl[{','.join(missing)}] but the project "
            "needs it — run `tuvl ship --force`"
        )
    return None


def ship_cmd(
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        "-d",
        help="Path to the tuvl project directory (contains config.yaml, models/, workflows/, etc.).",
        exists=True,
        file_okay=False,
    ),
    tag: str = typer.Option(
        None, "--tag", "-t", help="Image reference to build. Defaults to <project>:<version>."
    ),
    no_image: bool = typer.Option(
        False,
        "--no-image",
        "--no-build",
        help="Write the Dockerfile and Helm chart but skip the docker build.",
    ),
    fmt: str = typer.Option(
        "image",
        "--format",
        help="image (a docker image and a Helm chart), archive (the runtime files as a project "
        "you run with uv) or both.",
    ),
    archive_format: str = typer.Option("tar.gz", "--archive-format", help="tar.gz or zip."),
    out: Path = typer.Option(Path("dist"), "--out", help="Where the archive is written."),
    allow_incomplete: bool = typer.Option(
        False, "--allow-incomplete", help="Ship although some spec tasks are not done."
    ),
    allow_dirty: bool = typer.Option(
        False, "--allow-dirty", help="Ship although the working tree has uncommitted changes."
    ),
    push: bool = typer.Option(False, "--push", help="Push the image after a successful build."),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing generated files (deploy/, .dockerignore)."
    ),
    strict: bool = typer.Option(False, "--strict", help="Treat validation warnings as errors."),
    extra: list[str] = typer.Option(
        [],
        "--extra",
        help="Also install this tuvl extra (mcp, redis, vector) — for a feature the project "
        "turns on in a way `tuvl ship` cannot see. Repeatable.",
    ),
    with_laya: bool | None = typer.Option(
        None,
        "--with-laya/--without-laya",
        help="Add a Laya decision server (laya-serve) to the Helm chart. "
        "Default: on when an AgentModel uses provider: laya.",
    ),
) -> None:
    """Package the project for production — validate, containerize, and emit a Helm chart."""
    project_dir = project_dir.resolve()
    if fmt not in ("image", "archive", "both") or archive_format not in ("tar.gz", "zip"):
        console.print(
            "[red]--format is image, archive or both; --archive-format is tar.gz or zip.[/red]"
        )
        raise typer.Exit(2)
    want_image, want_archive = fmt in ("image", "both"), fmt in ("archive", "both")
    if not (project_dir / "pyproject.toml").exists():
        console.print(
            "[red]No pyproject.toml found — `tuvl ship` needs the project's dependency "
            "declaration to build the image. Run it from a `tuvl init` project root.[/red]"
        )
        raise typer.Exit(1)

    slug, version = _project_meta(project_dir)
    image = tag or f"{slug}:{version}"
    include_laya = laya.uses_laya(project_dir) if with_laya is None else with_laya

    console.print(
        Panel(
            f"Shipping [bold]{slug}[/bold] v{version}\n"
            f"Project: [bold]{project_dir}[/bold]\n"
            f"Image: [cyan]{image}[/cyan]",
            title="[blue]tuvl ship[/blue]",
            border_style="blue",
        )
    )

    console.print("\n[bold]Step 1/3 — validate[/bold]")
    result, known_models, known_agents, known_llms = run_validation(project_dir)
    for issue in result.issues:
        if issue.code in _SHIP_ERRORS:  # warnings in development, errors at ship time
            issue.severity = "error"
    if result.errors or (strict and result.warnings):
        console.print()
        _print_report(result, project_dir, known_models, known_agents, known_llms)
        console.print("[red bold]Ship aborted — fix validation issues first.[/red bold]")
        raise typer.Exit(1)
    warn_note = f" ({len(result.warnings)} warning(s))" if result.warnings else ""
    console.print(f"[green]✓ Validation passed[/green]{warn_note}")
    locked_version = _check_lock(project_dir)
    extras = _resolve_extras(project_dir, tuple(extra))
    model_vars = sorted({v for info in result.models.values() for v in info.get("env") or []})
    from tuvl.core import envconfig

    secret_entries, plain_entries = envconfig.ship_variables(project_dir)
    secret_vars = sorted(
        {e.var.name for e in secret_entries} - {"TUVL_SIGNING_KEY", "POSTGRES_PASSWORD"}
    )
    needed_vars = sorted({*model_vars, *secret_vars})
    secrets = ["TUVL_SIGNING_KEY (tuvl generate key)", "POSTGRES_PASSWORD", *needed_vars]
    described = {e.var.name: e.var for e in (*secret_entries, *plain_entries)}

    console.print("\n[bold]Step 2/3 — deploy artifacts[/bold]")
    deploy = project_dir / "deploy"
    chart = deploy / "chart" / slug
    laya_en = laya.english_only(project_dir)
    laya_tag = laya.image_tag(project_dir)
    storage_mounts = _local_storage_mounts(project_dir)
    sub = {
        "storage_enabled": "true" if storage_mounts else "false",
        "storage_mounts": json.dumps(storage_mounts),
        "name": slug,
        "version": version,
        "laya_enabled": "true" if include_laya else "false",
        "laya_repository": laya.LAYA_IMAGE_REPOSITORY,
        "laya_tag": laya_tag,
        "laya_memory_request": "2Gi" if laya_en else "4Gi",
        "laya_memory_limit": "3Gi" if laya_en else "5Gi",
        "laya_memory_note": "One checkpoint (english) resident takes about 2 GiB."
        if laya_en
        else "Both bundled checkpoints resident take about 3.5 GiB.",
        "model_secrets": ", ".join(needed_vars) or "none — no workflow uses a model",
        "extra_env": "".join(
            f"  {e.var.name}: {json.dumps(e.var.default)}\n"
            for e in plain_entries
            if e.var.default is not None
        ),
    }
    files: dict[Path, str] = {
        deploy / "Dockerfile": tpl.DOCKERFILE.replace("@TUVL_VERSION@", locked_version)
        .replace("@TUVL_EXTRAS@", f"[{','.join(extras)}]" if extras else "")
        .replace("@TUVL_EXTRA_NAMES@", ",".join(extras)),
        project_dir / ".dockerignore": tpl.DOCKERIGNORE,
        chart / "Chart.yaml": Template(tpl.CHART_YAML).substitute(sub),
        chart / "values.yaml": Template(tpl.VALUES_YAML).substitute(sub),
        chart / "templates" / "_helpers.tpl": tpl.HELPERS_TPL,
        chart / "templates" / "deployment.yaml": tpl.DEPLOYMENT_YAML,
        chart / "templates" / "service.yaml": tpl.SERVICE_YAML,
        chart / "templates" / "storage.yaml": tpl.STORAGE_YAML,
        chart / "templates" / "laya.yaml": tpl.LAYA_YAML,
        chart / "templates" / "NOTES.txt": tpl.NOTES_TXT,
    }
    # Warn before writing: if Chart.yaml is about to be kept (exists, no --force)
    # and its appVersion is stale, the deploy would reference an unbuilt tag.
    if not force:
        _warn_chart_version_skew(chart / "Chart.yaml", version)

    written: list[str] = []
    kept: list[str] = []
    for path, content in files.items():
        _write(path, content, force=force, project_dir=project_dir, written=written, kept=kept)
    for rel in written:
        console.print(f"  [green]wrote[/green] {rel}")
    problems = _check_image_context(project_dir)
    if any(rel.endswith("Dockerfile") for rel in kept):
        stale = _stale_dockerfile(deploy / "Dockerfile", extras)
        if stale:
            console.print(f"  [red]✗[/red] {escape(stale)}")
            console.print("[red bold]Ship aborted — the image would miss a feature.[/red bold]")
            raise typer.Exit(1)
    for rel in kept:
        console.print(f"  [dim]kept existing {rel} (--force to overwrite)[/dim]")
    values_kept = any(rel.endswith("values.yaml") for rel in kept)
    if values_kept:
        console.print(
            f"  [yellow]![/yellow] Laya is {'on' if include_laya else 'off'} for this ship, but the "
            "kept values.yaml decides — set [bold]laya.enabled[/bold] there or use --force"
        )
    elif include_laya:
        console.print(
            f"  [green]laya[/green] decision server: {laya.LAYA_IMAGE_REPOSITORY}:{laya_tag}"
            " (laya.enabled in values.yaml)"
        )

    if problems:
        for problem in problems[:20]:
            console.print(f"  [red]✗ V027[/red] {problem}")
        if len(problems) > 20:
            console.print(f"  [red]… and {len(problems) - 20} more[/red]")
        console.print(
            "[red bold]Ship aborted — the image would contain development material.[/red bold]"
        )
        raise typer.Exit(1)

    commit = _run_gate(
        project_dir,
        allow_incomplete=allow_incomplete,
        allow_dirty=allow_dirty,
        strict_lock=want_archive,
    )

    from tuvl import shipgate

    image_files = _image_files(
        project_dir,
        _ignore_rules(project_dir / ".dockerignore")
        if (project_dir / ".dockerignore").exists()
        else [],
    )
    runtime_hash = shipgate.runtime_hash(project_dir, image_files)
    console.print(
        f"[green]✓ runtime files hash[/green] {runtime_hash[:16]}… ({len(image_files)} files)"
    )
    console.print("\n[bold]Step 3/3 — package[/bold]")
    built_archive = None
    if want_archive:
        built_archive = _build_archive(
            project_dir,
            slug=slug,
            version=version,
            locked_version=locked_version,
            extras=extras,
            files=image_files,
            variables=sorted({*needed_vars, *(e.var.name for e in plain_entries)}),
            described=described,
            runtime_hash=runtime_hash,
            commit=commit,
            out_dir=out if out.is_absolute() else project_dir / out,
            fmt=archive_format,
        )
    if not want_image or no_image:
        console.print("[dim]Image skipped.[/dim]")
    else:
        docker = shutil.which("docker")
        if not docker:
            console.print(
                "[red]docker not found on PATH — install Docker or re-run with --no-build.[/red]"
            )
            raise typer.Exit(1)
        build = subprocess.run(
            [
                docker,
                "build",
                "-f",
                str(deploy / "Dockerfile"),
                "--label",
                f"io.tuvl.runtime-hash={runtime_hash}",
                "--label",
                f"io.tuvl.git-commit={commit}",
                "-t",
                image,
                ".",
            ],
            cwd=project_dir,
        )
        if build.returncode != 0:
            console.print("[red bold]docker build failed.[/red bold]")
            raise typer.Exit(build.returncode)
        if push:
            pushed = subprocess.run([docker, "push", image], cwd=project_dir)
            if pushed.returncode != 0:
                console.print("[red bold]docker push failed.[/red bold]")
                raise typer.Exit(pushed.returncode)

    built_note = " [dim](not built)[/dim]" if (no_image or not want_image) else ""
    archive_line = (
        f"Archive: [cyan]{built_archive.path}[/cyan]\n  sha256 {built_archive.sha256}\n"
        if built_archive
        else ""
    )
    from tuvl import audit

    audit.emit(
        project_dir,
        "ship.packaged",
        command="ship",
        entities=[slug],
        detail=f"{slug} {version}",
        refs={
            "runtime_hash": runtime_hash,
            "git_commit": commit,
            "format": fmt,
            "archive_sha256": built_archive.sha256 if built_archive else "",
            "image": image if want_image and not no_image else "",
        },
    )
    console.print(
        Panel(
            f"Image: [cyan]{image}[/cyan]{built_note}\n{archive_line}"
            f"Chart: [cyan]deploy/chart/{slug}[/cyan]\n\n"
            f"Deploy:\n"
            f"  [bold]kubectl create secret generic {slug}-env --from-env-file=.env[/bold]\n"
            f"  [bold]helm install {slug} deploy/chart/{slug} "
            f"--set image.repository=<registry>/{slug}[/bold]\n"
            f"  [dim]separate API and worker pods:[/dim] [bold]--set split=true "
            f"--set worker.replicaCount=3[/bold]\n"
            + (
                "  [dim]Laya decision server:[/dim] included; it needs ~4 GiB "
                "([bold]--set laya.enabled=false[/bold] to leave it out)\n"
                if include_laya
                else ""
            )
            + "\n"
            f"[dim]The secret must include: {', '.join(secrets)}.\n"
            "A production engine refuses to start while a model's variable is missing.\n"
            "The container runs in production mode: no dev routes, no Insight UI,\n"
            "JSON logs, telemetry on.[/dim]",
            title="[green]shipped[/green]",
            border_style="green",
        )
    )


def _run_gate(
    project_dir: Path, *, allow_incomplete: bool, allow_dirty: bool, strict_lock: bool
) -> str:
    """The deterministic gate before anything is packaged; returns the git commit being shipped."""
    from tuvl import shipgate
    from tuvl.review import review_project

    errors: list[str] = []
    gate = shipgate.spec_gate(project_dir, allow_incomplete=allow_incomplete)
    errors += gate.errors
    for w in gate.warnings:
        console.print(f"  [yellow]![/yellow] {escape(w)}")
    spec_driven = (project_dir / "specs").is_dir() and any((project_dir / "specs").glob("*.md"))
    if spec_driven:
        stale = shipgate.codegen_current(project_dir)
        if stale:
            errors.append(
                f"generated files are out of date ({', '.join(stale[:4])}): run `tuvl generate code`"
            )
    review = review_project(project_dir, with_ruff=False)
    errors += [f"review: {f.text()}" for f in review.errors]
    why = shipgate.uv_lock_current(project_dir)
    if why:
        (errors if strict_lock else []).append(why)
        if not strict_lock:
            console.print(f"  [yellow]![/yellow] {escape(why)}")
    commit, git_errors = shipgate.git_state(project_dir, allow_dirty=allow_dirty)
    errors += git_errors
    if errors:
        for line in errors[:20]:
            console.print(f"  [red]✗[/red] {escape(line)}")
        if len(errors) > 20:
            console.print(f"  [red]… and {len(errors) - 20} more[/red]")
        console.print("[red bold]Ship aborted — the gate failed.[/red bold]")
        raise typer.Exit(1)
    for note in gate.notes:
        console.print(f"  [dim]{escape(note)}[/dim]")
    console.print(
        "[green]✓ gate passed[/green]"
        + (f" (git {commit})" if commit else " [dim](not a git repository)[/dim]")
    )
    return commit


def _build_archive(
    project_dir: Path,
    *,
    slug: str,
    version: str,
    locked_version: str,
    extras: list[str],
    files: list[str],
    variables: list[str],
    described: dict[str, Any],
    runtime_hash: str,
    commit: str,
    out_dir: Path,
    fmt: str,
) -> shiparchive.Built:
    from tuvl import shiparchive

    pin = f"tuvl[{','.join(extras)}]=={locked_version}" if extras else f"tuvl=={locked_version}"
    locked = "uv.lock" in files
    generated = {
        "RUN.md": shiparchive.run_md(
            slug=slug, version=version, tuvl_pin=pin, runtime_hash=runtime_hash, commit=commit
        ),
        "run.sh": shiparchive.run_script(tuvl_pin=pin, locked=locked),
        ".env.example": shiparchive.env_example(variables, described),
        "BUILD-INFO.json": shiparchive.build_info(
            project=slug,
            version=version,
            tuvl=locked_version,
            extras=extras,
            git_commit=commit,
            runtime_hash=runtime_hash,
            files=len(files),
        ),
    }
    built = shiparchive.build(
        out_dir,
        name=f"{slug}-{version}",
        root=project_dir,
        files=files,
        generated=generated,
        fmt=fmt,
    )
    shown = (
        built.path.relative_to(project_dir)
        if built.path.is_relative_to(project_dir)
        else built.path
    )
    console.print(
        f"[green]✓ archive[/green] {shown} ({built.files} files, sha256 {built.sha256[:16]}…)"
    )
    return built
