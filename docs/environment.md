# Environment variables

A tuvl project reads its environment in three ways: the engine's own settings (`TUVL_*`, `POSTGRES_*`),
`${VAR}` references in its YAML, and `os.environ` reads in its Python. `kind: EnvConfig` is where a project
says what it needs, so that `tuvl ship`, Insight and the production boot all agree.

## Declaring variables

```yaml
kind: EnvConfig
spec:
  variables:
    - name: GEMINI_API_KEY
      description: Gemini key for the triage model
      secret: true
      required: true
    - name: ESCALATION_SCORE
      description: Score above which a ticket escalates
      default: "0.8"
```

| Field | Meaning |
|---|---|
| `name` | The variable, such as `CRM_TOKEN`. |
| `description` | Shown in Insight and written above the variable in `.env.example`. |
| `secret` | A secret is declared, never given a value in YAML: its value lives in `.env` locally and in the cluster Secret. A secret cannot have a `default`. |
| `required` | A production server refuses to start while it is unset. `tuvl check system` fails on it in development. A required variable cannot have a `default`. |
| `default` | The value used when nothing sets the variable. Plain variables only; it also goes into the chart values. |
| `example` | Shown as the placeholder in Insight. |

Put it anywhere under the project (`config/env.yaml` is where Insight creates it). The order of
resolution is the engine's own: the real environment, then `.env`, then the declared default.
Knobs that `.tuvl/system.yaml` already holds (`spec.api.*`) stay there; the manifest describes a variable, it
does not replace a setting.

The engine's own variables (about forty, among them `TUVL_ENV`, `TUVL_SIGNING_KEY`, `POSTGRES_*`,
`TUVL_CORS_ORIGINS` and the first-boot `TUVL_BOOTSTRAP_ADMIN_*`) are built in, with a description and
whether they are secret, so you declare only your own.

## What the project is checked against

`tuvl lint project` reads every `${VAR}` in your YAML (and the literal `os.environ[...]` /
`os.environ.get(...)` / `os.getenv(...)` reads in your Python outside `tests/`):

- **V045** the `EnvConfig` is malformed, or a variable is declared twice, or a secret or required variable has a default;
- **V046** a variable is used but declared nowhere, **once the project has an `EnvConfig`**;
- **V047** a variable is declared but nothing refers to it (warning).

A project without an `EnvConfig` is not held to V046 or V047, so adding the file is a choice.

## Insight

**Settings → Environment** (development only) lists the engine's variables, yours and every `${VAR}`
found in the YAML, each marked set, missing or undeclared.

- Saving writes the project's `.env` (created with mode 0600; your other lines and comments are kept).
- A secret's value is **write-only**: it is written, and never returned or shown again.
- A change reaches the dev server at its next start.
- **Declare** adds an undeclared variable to your `EnvConfig`.
- Insight warns when `.env` is not in `.gitignore`.

## Shipping

`tuvl ship` builds from the manifest instead of a models-only list:

- the Secret list it prints, and the comment in the chart's `values.yaml`, name every secret;
- non-secret variables with a `default` are written into the chart's `env:` block;
- `.env.example` in the archive carries each variable with its description, and a plain variable's default.

An existing `values.yaml` is kept unless you pass `--force`, as before.

## Production

A production server refuses to start while a variable declared `required` is unset, and names every one
that is missing. Models are checked the same way (a missing provider key stops the start); the
manifest extends that to any variable you decide the project cannot run without.
