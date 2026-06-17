# Changelog

All notable changes to **tuvl** are recorded here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project follows [PEP 440](https://peps.python.org/pep-0440/) versioning
(`YEAR.MAJOR.MINOR` with `bN` / `aN` / `rcN` pre-release suffixes), with a
semver-shaped git tag form (`v2026.2.3-beta.1`) that is normalised at publish
time. See `release-please-config.json` for the automation contract.

---

## [2026.2.3b1] — 2026-06-15

> **Public-beta launch.** First public artifact on PyPI / npm / tuvl.dev /
> tuvl.io. Coordinated release across four surfaces — every package
> publishes at the same human-readable version string.
>
> **Note on the version jump.** The intermediate `2026.2.1-beta.2` was
> reserved for the TypeScript SDK (`@tuvl/client@2026.2.1-beta.2` had
> already been published on npm by the time the engine release was being
> cut). To keep the version string aligned across PyPI and npm registries
> going forward, the engine starts its public release series at
> `2026.2.3-beta.1`; later releases will track from there.

### Coordinated surfaces

| Surface | Package | Version | Channel |
|---|---|---|---|
| Engine | `tuvl` | `2026.2.3b1` | PyPI |
| Developer portal | `tuvl-insight` | `2026.2.3b1` | PyPI (via `tuvl[standard]`) |
| TypeScript SDK | `@tuvl/client` | `2026.2.3-beta.1` | npm (publishes alongside this release) |
| Documentation | `tuvl.dev/beta/` | `2026.2.3-beta.1` | GitHub Pages (mike) |
| Marketing site | `tuvl.io` | `v2026.2.3-beta.1` | static |

### Security

- **PII masking now applied to every streamed `StepEvent`.** `mask_secure_data`
  is invoked at every snapshot-yield site — the workflow-engine SSE / gRPC
  stream, the CLI test runner, the Spectrum debug trace, and the Spectrum
  final-state frame on the gRPC stream. Fields declared `secure: true` in any
  `ModelDefinition` arrive at SDK consumers as the literal string `"*****"`.
  Previously the masking helper was applied only on the OpenTelemetry-span
  path; values reached real-time stream subscribers (including the Tuvl
  Insight Spectrum debugger UI) in plaintext.
- **gRPC token-expiry enforcement.** A new transport-neutral
  `enforce_token_security` helper in `auth/biscuit_auth.py` is now invoked
  from `grpc/iam_servicer._verify_biscuit` and
  `grpc/servicer._verify_raw_token`. Both gRPC servicers now run
  `authorizer.authorize()` with `set_time()`, honouring embedded
  `check if time(...)` rules and the `exp()` fact — identical to the REST
  `verify_token` contract. Previously only the cryptographic signature was
  validated on gRPC, so expired tokens silently went through.
- **Dev sentinel hard-fail in production.** `TUVL_DEV_MODE=true` combined
  with `TUVL_ENV=production` is now refused at boot regardless of
  `TUVL_ALLOW_DEV_AUTH`. Closes a two-env-var footgun where a leaked dev
  flag on a prod host could silently activate the `TUVL_DEV_API_KEY`
  `iam:admin` superuser bypass.
- **`TUVL_REQUIRE_REDIS` fail-closed mode.** New setting (default `false`).
  When `true`, a configured Redis datasource that fails to connect raises
  `RedisRequiredButUnavailableError` during startup instead of silently
  degrading to per-process memory. Recommended for production deployments
  that rely on cross-worker token revocation.
- **CIDR allowlist fail-loud.** `parse_cidr_list` and the `--allow-host`
  parser now raise `InvalidAllowedHostError` on unparseable entries instead
  of silently dropping them (`contextlib.suppress(ValueError)` removed). A
  non-strict mode remains available for diagnostic tooling.

### Added

- **+38 regression tests.** New suites pin the runtime-enforcement promises
  from `docs/TUVL_AGENTIC_MANUAL.md`:
  - `tests/core/test_masking.py` — masking helper contract + source-level
    pins on every snapshot yield site.
  - `tests/core/test_grpc_token_expiry.py` — covers fresh / expired /
    garbage tokens on both gRPC servicers and the new helper.
  - `tests/core/test_dev_sentinel.py` — subprocess tests of all sentinel
    paths (production refusal with/without ack flag, dev-mode without
    ack, dev-mode with ack, inert-when-off).
  - `tests/core/test_golden_rules.py` — Golden Rule 5 (`context.models`
    allowlist), Rule 6 (unmapped non-default signal raises), Rule 17
    (schema-version pin mismatch raises).
  - `tests/core/test_runtime_guards.py` — source-level pins for CRUD
    scope wiring, RAG tenant filter, and the blacklist multi-worker
    docstring warning.
- **Public-release metadata.** `LICENSE` (MIT), `AGENT.md` (condensed agent
  onboarding), `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `SECURITY.md`, and
  the GitHub bug-report / feature-request / pull-request templates under
  `.github/`.
- **CI Postgres service.** `ci.yml` now boots `postgres:16-alpine` and
  exports `POSTGRES_*` env vars so future integration tests can hit a real
  database. Current unit tests are unaffected (in-memory aiosqlite still).
- **CI wheel smoke test.** `publish.yml` now installs the built artefact in
  a fresh venv between `uv build` and the PyPI upload, runs `tuvl --version`,
  and fails the build if the reported version does not match the tag.
- **`make test` / `make test-verbose` / `make typecheck` targets.**
- **`make setup-min` target** for production-style installs that don't need
  Insight or dev tools.
- **`secure_fields_view()` accessor** in `tuvl.core.models.loader` — returns
  an immutable `frozenset` snapshot of `SECURE_FIELDS` for callers that
  need stability across a concurrent hot reload.
- **`release-please` manifest mode.** Adds `.release-please-manifest.json`
  + `release-please-config.json` with explicit `prerelease-type: "b"` for
  the `bN` PEP 440 scheme. The previous bare `release-type: python` did
  not natively understand the project's prerelease format.
- **`scripts/manual/grpc_smoke.py`** — relocated from the repo root with a
  clearer docstring documenting it as a hand-run smoke check.
- **`TokenExpiredError`** exception type in `auth/biscuit_auth.py`, raised
  by the new transport-neutral `enforce_token_security` helper and
  translated by REST and gRPC sites into 401 / `UNAUTHENTICATED`.
- **`RedisRequiredButUnavailableError`** exception type, raised by
  `init_redis` when `TUVL_REQUIRE_REDIS=true` and Redis is unreachable.

### Changed

- **`make setup` now installs dev tools by default** (`uv sync --extra
  standard --extra dev`). A fresh clone followed by `make setup && make
  test` now works without additional flags. The previous lightweight
  behaviour is preserved as `make setup-min`.
- **`make check` is now ruff-only** (lint + format check). Mypy moved to
  the advisory `make typecheck` target with `|| true` so the ~190
  pre-existing type errors in the protobuf-generated and SQLModel
  dynamic-class paths don't gate CI. Drive the backlog down between
  releases.
- **`tuvl/core/__init__.py` now exposes `app` lazily** via PEP 562
  `__getattr__`. Submodule imports (e.g.
  `from tuvl.core.engine.runner import StepEvent`) no longer trigger a
  full FastAPI app boot. Behaviour at first attribute access is identical
  to the previous module-level import.
- **`auth/keys.py` `key_manager` is now lazy.** The module-level
  `key_manager = TuvlKeyManager()` singleton has been replaced with a
  `get_key_manager()` accessor plus module `__getattr__`. Importing
  `tuvl.core.auth.schemas` in a cold environment no longer raises
  `RuntimeError("TUVL_BISCUIT_PRIVATE_KEY must be set")`.
- **`publish.yml` tag-normalisation extended** from beta-only to handle
  `-alpha.N`, `-beta.N`, and `-rc.N` consistently across the build and
  smoke-test steps.
- **REST `verify_token` refactored** from 40 inline lines of expiry
  enforcement to 5 lines calling the new `enforce_token_security` helper.
  Behaviour is unchanged; gRPC and REST now share a single source of
  truth for token-side checks.
- **Top-level `tuvl/__init__.py` now exposes `__version__`** via
  `importlib.metadata`. `import tuvl; tuvl.__version__` works without any
  engine boot.
- **Manual changelog (`docs/TUVL_AGENTIC_MANUAL.md` §8)** records the
  version bump to `b2`. The prior `b1` changelog entry is preserved as
  historical record.

### Fixed

- **Three `.DS_Store` files untracked.** They had been committed before
  the `**/.DS_Store` ignore rule was hardened. `git rm --cached` plus
  the existing rule now keeps them out for good.
- **`ruff format --check src/` now passes.** Cleared 14 files that the
  formatter had flagged unformatted (`runner.py`, `agent_runner.py`, both
  `*_pb2*.py`, `tuvl_insight/__init__.py`, and nine others).
- **Stripped 40 stale `# type: ignore` comments** across 27 files that
  mypy flagged as unused.
- **`tests/core/test_auth_router.py` in-memory SQLite setup** — the IAM
  router test now narrows `SQLModel.metadata.create_all` to the
  `IAMUser.__table__` so it doesn't try to create every loaded
  `ModelDefinition` against the in-memory DB. `aiosqlite>=0.20` is now a
  declared dev dependency.

### Removed

- **`AGENTS.md`** (46 lines) — replaced by the condensed `AGENT.md`.
- **`KNOWME.md`** (985 lines) — folded into the new ground-truth
  `docs/TUVL_AGENTIC_MANUAL.md`.
- **`test_grpc.py`** at the repo root — moved to `scripts/manual/grpc_smoke.py`.
- **Three tracked `.DS_Store`** files (see "Fixed").

### Compatibility & dependency floor

- Python: `>= 3.13`.
- Postgres: `16+` recommended.
- Key runtime pins in `pyproject.toml`:
  - `litellm[google] == 1.86.2`
  - `pydantic == 2.13.4`
  - `pydantic-settings == 2.14.1`
  - `python-dotenv == 1.2.2`
  - `typer == 0.26.5`
- TypeScript SDK peer deps (only when using `mode: "grpc"`):
  `@protobuf-ts/grpcweb-transport`, `@protobuf-ts/runtime-rpc`.

### Behaviour changes that may affect early adopters of the internal `b1`–`b12` iterations

1. Snapshot values reaching `onProgress` callbacks will show `"*****"`
   for fields declared `secure: true`. Adjust downstream telemetry parsers
   accordingly.
2. Expired Biscuit tokens are now rejected on gRPC just as on REST.
   Anything previously relying on the (broken) lenient gRPC behaviour
   needs to refresh on time.
3. `make setup` now installs dev tools by default. Production from-source
   installs should switch to `make setup-min`.
4. `make check` no longer runs mypy. Use `make typecheck` or invoke mypy
   directly.
5. `TUVL_ENV=production` with `TUVL_DEV_MODE=true` now hard-fails at boot.
   Unset `TUVL_DEV_MODE` in production environments.

### Known limitations

- Approximately 190 pre-existing mypy errors remain in the codebase
  (advisory only; tracked as a post-beta cleanup milestone).
- Engine test ratio is ~21% (test LOC / src LOC). Critical paths covered;
  some Golden Rule enforcement is pinned only at the source level until
  end-to-end fixtures land.
- Postgres integration tests not yet exercising the wired CI service.
- `release-please` config has not been sandbox-tested for the `bN` PEP 440
  scheme. `publish.yml` does not depend on release-please — tag pushes
  trigger publishing directly — so any release-please misbehaviour on the
  first run will not block the tagged release.
- TypeScript SDK JSDoc / README does not yet mention the strengthened
  snapshot masking semantics. Will land in the next SDK release; the
  canonical README on GitHub is updated separately.

### Acknowledgements

Prepared after a structured pre-release audit covering functionality,
security, code quality, public-release hygiene, and testing posture. The
five P0 release-blockers identified by that audit — the PII leak, the
gRPC auth bypass, the broken fresh-clone install path, the tracked OS
artifacts, and the format-gate failure on the base branch — are all
fixed in this release.

— Sooraj Rajagopalan, tuvl.io


[2026.2.3b1]: https://github.com/tuvl-io/tuvl/releases/tag/v2026.2.3-beta.1
