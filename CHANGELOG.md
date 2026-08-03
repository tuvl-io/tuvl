# Changelog

All notable changes to **tuvl** are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project follows [Semantic Versioning](https://semver.org) as of `1.0.0`.
See `release-please-config.json` for the automation contract.

---

## [1.0.1] — 2026-08-03

Patch release: correctness fixes only. No API or schema changes.

### Added

- Model fields accept generated-default literals `default: now`
  (`timestamp` / `timestamptz`) and `default: today` (`date`), mirroring the
  existing `default: uuid4`. Use them to opt a field into a server-generated
  value on create.

### Fixed

- **Nullable `uuid` / `date` / `timestamp` / `timestamptz` fields no longer
  receive a silent generated value on create.** The model loader treated *any*
  default-less field of these types as "auto-generate a value", so a nullable
  `uuid` foreign key persisted a dangling random UUID, and a default-less
  `date` / `timestamp` persisted `today` / `now()` instead of `NULL` — on every
  row created through ModelOp CRUD (raw-SQL nodes were unaffected, which is why
  the corruption stayed hidden). Generation is now limited to the cases where it
  is safe and intended: a `uuid` **primary key**, and the `created_at` /
  `updated_at` audit-column convention. Every other default-less field of these
  types now persists as `NULL` (or is required when declared `required: true`).

  **Migration.** Any field that (perhaps unintentionally) relied on the old
  implicit `today` / `now` / `uuid` now stores `NULL` — which is almost always
  what the YAML author already believed was happening, given these fields carry
  no default. To keep a generated value, opt in explicitly: `default: uuid4`,
  `default: now`, or `default: today` — or, for timestamps, rename the column to
  `created_at` / `updated_at`. `uuid` primary keys and `created_at` /
  `updated_at` are unchanged.

- CLI commands no longer emit the `Settings loaded` startup log. Constructing
  `Settings` on import logged at info level, so quick commands (e.g.
  `tuvl --version`) printed a log line alongside their output. The log moved to
  server boot — `tuvl run` / `tuvl dev` still log it — and the routine
  `Project config loaded` line dropped to debug.

## [1.0.0] — 2026-07-26

First official stable release. Establishes the SemVer baseline and folds in
scheduled triggers, an API-security hardening pass, and a full internal
quality sweep. Everything below is the state of the engine at first publish.

### Added

- **Scheduled workflow triggers.** `spec.trigger.schedule: "<5-field cron>"`
  (UTC, 1-minute granularity) runs a workflow on a schedule, alongside or
  instead of an HTTP `path`. Skip-on-miss (no catch-up); single-fire across
  workers via a Postgres advisory lock; scheduled runs execute with no
  principal. Knob `TUVL_SCHEDULER_ENABLED` (default on); single-tenant only.
  Authorable from the Insight workflow trigger panel.
- **Global CRUD kill-switch.** `spec.api.expose_model_crud: false` in
  `.tuvl/system.yaml` (`SystemConfig`) unmounts every auto-generated
  `/models/*` route — the routes are absent, not merely denied — leaving only
  workflow APIs. `TUVL_EXPOSE_MODEL_CRUD` overrides the file. Editable from the
  Insight Settings "API Access" section. Restart-required.
- **Per-model group ACLs.** `spec.access.{read,write,delete}_groups` on a
  `ModelDefinition` pins CRUD tiers to IAM groups: a caller needs the scope
  **and** group membership (`iam:admin` bypasses). Undeclared tiers cascade
  restrictively (write→read, delete→write).
- **`spec.trigger.public: true`** opts a workflow into anonymous access (see
  the default-deny change below). Workflow manifests expose the `public` flag;
  `@tuvl/client`'s `WorkflowManifest` carries it.
- `tuvl validate` / `tuvl ship` now discover config by the same recursive,
  `kind`-dispatched walk the runtime uses — YAML in any subdirectory and `.yml`
  files are validated, and `EmbeddingRegistry`/`EmbeddingConfig`,
  `CollectionRegistry`/`CollectionConfig`, `RedisConfig`, and
  `FederationProvider` are now checked. An unknown `kind:` is a validation error.
- `tuvl stream-watch --timeout` (default: no read-timeout on the event stream).

### Changed

- **BREAKING — workflow triggers deny by default in production.** Every trigger
  route (REST, the versioned `/{schema_version}/run/{name}` route, and gRPC
  `RunWorkflow`) now requires a valid bearer token by default, even when the
  workflow declares no `required_scope`/`required_group`. Anonymous access is an
  explicit opt-in via `spec.trigger.public: true`. Dev mode (`tuvl dev`) exempts
  unscoped workflows so quickstarts run tokenless. **Migration:** add
  `spec.trigger.public: true` to any workflow (e.g. user-registration endpoints)
  that must remain callable without a token; combining it with
  `required_scope`/`required_group` is a validation error.
- **Default engine port is now `8885`** (was `8000`) for `tuvl dev` and
  `tuvl run`, the Insight portal (`http://localhost:8885/insight/`), the `tuvl
  ship` container/Helm chart, and the Insight dev proxy. 8885 spells "TUVL" on a
  phone keypad (T·U·V=8, L=5) and avoids the crowded 8000/8080/8888 range.
  Override with `--port` or `TUVL_DEV_PORT`.
- **Semantic Versioning.** Releases follow SemVer (`MAJOR.MINOR.PATCH`):
  breaking changes bump the major, features the minor, fixes the patch.
- The REST and gRPC IAM surfaces are now thin adapters over one transport-neutral
  service (`core/auth/iam_service.py`) — identical behavior on both transports
  (see Security).
- Models without an explicit `spec.datasource` bind to the primary `DataSource`
  (the one with `metadata.primary: true`) rather than a hardcoded name, so a
  scaffolded project creates its tables out of the box.
- `tuvl ship` Dockerfiles float the engine to the latest release the project's
  `pyproject.toml` allows (`uv sync --upgrade-package tuvl`); other dependencies
  stay lockfile-pinned. `tuvl ship` warns when a kept `Chart.yaml` `appVersion`
  has gone stale against the build version.

### Fixed

- Diagnostic logs now go to **stderr**, keeping stdout clean for actual command
  output. Previously the log stream (including the import-time "Settings loaded"
  line) was written to stdout and interleaved with results — e.g. `tuvl --version`
  emitted a JSON log line before the version, breaking anything parsing it.
- `tuvl db generate-rls` / `check-rls` now load the target project, so tenant
  model tables appear (previously only system tables were emitted).
- A `ModelOp` database error now returns the workflow's declared error-route
  response instead of an unhandled 500, and no longer leaks the SQL statement or
  bound parameters into the error body.
- HumanInTheLoop suspension works under the `tuvl dev` session key (the dev
  principal is now a stable UUID).
- The Insight workflow editor no longer corrupts version-pinned
  `context: {models: [...]}` to `'[object Object]'` on save.
- `tuvl validate --strict` no longer false-warns on projects that legitimately
  have no custom node files.
- Per-model / rotated LLM credentials take effect immediately (resolved as
  per-call parameters instead of a process-global env mutation).
- The engine fails fast at startup when `TUVL_BISCUIT_PRIVATE_KEY` is missing in
  production, rather than booting and erroring on the first request.
- Crashed workers no longer leave phantom agent runs in the operator API
  (Redis mirror entries expire).

### Security

- Production default-deny on workflow triggers; per-model group ACLs; the CRUD
  kill-switch (above).
- REST/gRPC IAM parity closed two gRPC-only gaps: login now runs the same
  constant-time dummy-hash verify (no user enumeration by response timing), and
  `GetMe`/`RefreshToken` run the same token authorization checks (a token
  missing its `user()` fact is now rejected instead of silently accepted).
  Federation path sanitization and file I/O aligned to the hardened REST path.
- Anonymous callers get a uniform `UNAUTHENTICATED` for unknown, disabled, and
  auth-required workflows across REST and gRPC, so the workflow namespace can't
  be probed without a credential.
- No signing key, no boot (above); the dev-mode superuser shortcut hard-exits
  if enabled in a `TUVL_ENV=production` process.

### Internal / Quality

- Transport-neutral IAM service (REST+gRPC), a single shared step-execution loop
  behind `run()`/`run_streaming()`, and the 6,724-line Insight workflow editor
  split into a reviewable module tree. Validator and runtime share one config
  discovery + document normalization path. New: a protobuf wire-conformance test
  tying the hand-written TS gRPC codecs to the `.proto` definitions, an Insight
  vitest suite + CI job, and a scoped mypy ratchet. 648 engine tests + 133 UI
  tests.

### Version map

| Surface | Package | Version | Channel |
|---|---|---|---|
| Engine | `tuvl` | `1.0.0` | PyPI |
| Developer portal | `tuvl-insight` | `1.0.0` | PyPI (via `tuvl[standard]`) |
| TypeScript SDK | `@tuvl/client` | `1.0.0` | npm |
| Documentation | `tuvl.io/docs` | `1.0.0` | GitHub Pages |
| Marketing site | `tuvl.io` | `v1.0.0` | static |
