# Authentication & Authorization

tuvl's auth stack is built on **Ed25519-signed JWTs** — offline-verifiable bearer tokens whose claims carry the caller's identity, IAM roles and scopes — backed by a small relational IAM (users, roles, scopes) and enforced by one shared guard on every REST entry point. Authentication answers *who is calling* (a verified `sub` claim); authorization answers *what they may do* (`scopes` and `groups` claims checked against per-resource requirements).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Token Anatomy](#2-token-anatomy)
3. [The Verification Pipeline](#3-the-verification-pipeline)
4. [Login & Identity](#4-login--identity)
5. [Federation Providers](#5-federation-providers)
6. [The IAM Model](#6-the-iam-model)
7. [Authorization Surfaces](#7-authorization-surfaces)
8. [One IAM Implementation](#8-one-iam-implementation)
9. [Dev Mode vs Production](#9-dev-mode-vs-production)
10. [Key Management](#10-key-management)
11. [Failure Modes](#11-failure-modes)
12. [Reading the Code](#12-reading-the-code)

---

## 1. Overview

Two planes, one token:

- **Authentication (who).** A caller presents a JWT in `Authorization: Bearer <token>`. The engine verifies its Ed25519 signature against the process key pair (`alg: EdDSA` only) and reads the `sub` claim. No database lookup is needed to verify a token — verification is fully offline.
- **Authorization (what).** The token also carries `scopes` (fine-grained permissions such as `candidate:read`) and `groups` (the caller's IAM role names). Each protected surface declares a required scope and/or group; enforcement is a set-membership check on the verified claims.

```
                 login (password / OAuth)                request
                          │                                 │
                          ▼                                 ▼
   IAM tables ──► mint_token ──► JWT ──────► verify signature (EdDSA), issuer, exp
   (roles →        sub/groups/                               │
    scopes)        scopes/iat/exp                            ▼
                                             revocation list · session end
                                                             │
                                                             ▼
                                                     authorize_token
                                                (required scope AND group,
                                                 iam:admin bypasses both)
```

Key properties:

- **Stateless verification.** The IAM database is consulted only at mint time. A token is a snapshot of the user's roles and scopes; role changes take effect on the next mint (login or refresh).
- **Revocation is the one stateful exception.** Logout and refresh push the token's SHA-256 hash onto a Redis-backed blacklist checked on every REST request, and narrowing a user's access ends their sessions (below).
- **One guard, every transport.** `verify_bearer_token` and `authorize_token` in `tuvl/core/auth/access.py` are the single source of truth for the FastAPI dependencies and MCP export.

---

## 2. Token Anatomy

Tokens are minted by `mint_token` in `tuvl/core/auth/token.py`: a JWT signed with the process's Ed25519 key (`alg: EdDSA`, header `kid` = the key id).

```json
{
  "iss": "tuvl",
  "sub": "3f6c…-uuid",                 // who the token belongs to
  "groups": ["hr_manager"],            // the user's IAM role names
  "scopes": ["candidate:read"],        // the scopes those roles grant
  "tenant": "acme",                    // multi-tenant deployments only
  "iat": 1760000000.123,               // issued at (seconds, ms precision)
  "exp": 1760086400,                   // expiry (omitted only with TTL 0)
  "jti": "9f…"                         // unique token id
}
```

Details that matter:

- **Expiry is signed.** With `ttl_seconds` set (default `TUVL_TOKEN_TTL_SECONDS`, 86400) the token carries `exp`; signature verification itself refuses an expired token.
- **Validated before signing.** Group and scope names must match `^[\w:.\-/]+$`, and a tenant id the database session allowlist; the same pattern is enforced by `require_scope` / `require_groups` when a guard is declared.
- **Readable, not secret.** A JWT's claims are base64, not encrypted: anyone holding the token can read them (that's how clients show who is signed in). Keep secrets and personal data out of claims.
- **The public key is published** at `GET /auth/jwks.json` (RFC 8037 OKP/Ed25519), so gateways and other services can verify tokens themselves. Offline verification can't see logouts or ended sessions — ask tuvl (`/auth/me`) when that matters.

---

## 3. The Verification Pipeline

Every REST request passes through the dependency chain in `tuvl/core/auth/access.py`:

```
Authorization header
    │
    ▼
verify_token / verify_bearer_token
    ├── 401 if no token
    ├── dev-mode shortcut: dev key → synthetic iam:admin claims   (§9)
    ├── decode_token: signature (EdDSA only), issuer, sub/iat/iss present, exp
    ├── token_blacklist.is_revoked(token)        ← Redis / in-process
    └── issued before the user's sessions ended? ← iat vs sessions_ended_at
            │
            ▼
get_current_user(claims) → TokenUser(user_id, groups, scopes, tenant_id)   (shape-checked)
            │
            ▼
authorize_token(claims, required_scope=…, required_groups=…)
```

**`decode_token`** accepts `EdDSA` only, so an `alg: none` token or an HMAC token "signed" with the public key is refused. It requires `iss`, `sub` and `iat`; a missing or wrong claim, a bad signature or an expired token is a 401. Claims are then shape-checked: `groups`/`scopes` must be lists of strings and `tenant` must pass the tenant allowlist.

**`authorize_token`** is the single scope/group decision point:

- A missing subject or a malformed claim → `TokenUnauthorizedError` (401 / `UNAUTHENTICATED`).
- When both a scope and a group are required, the token must satisfy **both** (AND, not OR). Within `required_groups`, membership in any one listed group suffices.
- **`iam:admin` is the superuser bypass** — a token carrying the `iam:admin` scope satisfies every scope and group requirement and returns immediately.
- Otherwise a missing scope/group → `TokenForbiddenError` (403 / `PERMISSION_DENIED`).

`require_scope("x:y")` and `require_groups([...])` are FastAPI dependency factories that wrap `authorize_token` and translate its errors to `HTTPException`s. They validate the scope/group names against the safe-character pattern at router-construction time.

**`bind_principal_context`** is an async dependency layered on `get_current_user` that writes the verified `user_id` (and `tenant_id`, when the token carries a `tenant` claim) into request-scoped ContextVars, so structlog and OTel spans annotate every record with the caller identity. Tenant binding is how opt-in multi-tenant deployments scope database sessions; single-tenant deployments can ignore it — the ContextVar simply stays unset.

Behavior is pinned by `tests/core/test_auth_router.py` and `tests/core/test_iam_rest.py`.

---

## 4. Login & Identity

All identity endpoints live in `tuvl/core/auth/router.py` under `/auth`.

### Bootstrap

`POST /auth/bootstrap` creates the first administrator. It is only available while `tuvl_system_iam_users` is empty (409 afterwards) and requires no token. It creates a `superadmin` role carrying `iam:admin`, assigns it to the new user, and returns a token.

### Password login

`POST /auth/token` is a standard OAuth2 password-grant form. The `username` field is the user's email address (a phone number also matches — lookup is `email OR phone_number`).

- **bcrypt off the event loop.** Hashing and verification run at cost factor 12 (~200–300 ms of pure CPU) and are always dispatched through `run_in_threadpool` (`aget_password_hash` / `averify_password` in `tuvl/core/auth/crypto.py`), so login never stalls the ASGI event loop. Passwords are truncated to bcrypt's 72-byte limit before hashing, matching bcrypt's own semantics.
- **Login-timing equalisation.** When the account is missing, inactive, or passwordless, the handler still verifies the submitted password against a throwaway hash (`_DUMMY_PW_HASH`), so response timing cannot be used to enumerate which emails exist. All failure cases return the same generic 401.

On success the handler loads the user's roles and the distinct union of their scopes, and mints a token (§6).

### Session lifecycle

| Endpoint | Behavior |
|---|---|
| `GET /auth/me` | Decodes the calling token; returns `user_id`, `groups`, `scopes`. |
| `POST /auth/refresh` | Re-checks the user is active, revokes the old token, mints a fresh one. |
| `POST /auth/logout` | Revokes the current token (204). |

**Ending a user's sessions.** Tokens carry their issue time (`iat`, ms). When a user's access
shrinks — deactivated, password changed, deleted, a role taken away, or a role they hold deleted or
stripped of a scope — the IAM service records *sessions ended at now* for each affected user, and
every token issued before that is refused (`401 Session ended — sign in again.`). The next sign-in
mints a token with the current roles. Granting a role or adding scopes ends nothing; it arrives at
the next sign-in. Tokens without `iat` (minted before this existed) count as older than any end.

Revocation (`tuvl/core/auth/blacklist.py`) stores the SHA-256 hex digest of the raw token — never the token itself — with a TTL matching the token lifetime, in Redis when a redis datasource is configured, otherwise in an in-process dict. **Multi-worker deployments must configure Redis**: without it, a token revoked on one worker is still accepted by the others.

There is no server-side session table; the token *is* the session.

---

## 5. Federation Providers

External IdPs are declared as YAML files in the project's `federation/` directory, loaded by `tuvl/core/auth/federation_loader.py`:

```yaml
kind: FederationProvider
version: v1
metadata:
  name: google              # matches /auth/oauth/google/…
enabled: true
spec:
  provider: google          # google | github | microsoft | custom
  client_id: ${GOOGLE_CLIENT_ID}
  client_secret: ${GOOGLE_CLIENT_SECRET}
  # Optional: auth_url / token_url / userinfo_url / scope overrides
  # tenant_id: common       # microsoft only
  # allowed_domains: [example.com]
  # default_role: member
```

`${VAR}` references are resolved from the environment at load time (`${VAR:default}` supports defaults); a provider whose variables are unset is skipped with a warning. Built-in URL defaults exist for Google, GitHub, and Microsoft. A legacy fallback reads `TUVL_OAUTH_<PROVIDER>_CLIENT_ID` / `_CLIENT_SECRET` settings when no YAML exists for a built-in provider.

The flow is OAuth 2.0 authorization-code:

1. `GET /auth/oauth/{provider}/start` — stores a one-time `state`, redirects to the IdP.
2. `GET /auth/oauth/{provider}/callback` — consumes the state, exchanges the code, fetches the user profile, and upserts an IAM user keyed by `(federated_provider, federated_sub)`.

Account-linking rules in the callback:

- **`email_verified` is required for auto-link.** If an account with the same email already exists but the IdP did not assert the email as verified, the callback returns 403 instead of linking — otherwise an attacker registering an unverified IdP identity under a victim's email could take over the account. For GitHub, only addresses from the verified `/user/emails` list are trusted.
- `allowed_domains`, when set, rejects any email outside the list (403).
- New federated users are created passwordless; `default_role`, when set and existing in the IAM database, is auto-assigned so first-time users don't arrive with zero permissions.

The callback returns the same kind of token as password login, or redirects to `TUVL_OAUTH_UI_REDIRECT_URL` with the token as a query param when configured. `TUVL_OAUTH_BASE_URL` sets the redirect-URI origin registered with the IdP.

Provider YAMLs can be managed at runtime via `/auth/admin/federation` (list/get/put/delete, `iam:admin` required); `client_secret` is redacted in read responses.

---

## 6. The IAM Model

Four tables, defined in `tuvl/core/auth/models.py`. All primary keys are UUIDs, safe to embed in tokens.

```
tuvl_system_iam_users                 tuvl_system_iam_roles
┌──────────────────────────┐          ┌──────────────────────┐
│ id (uuid, pk)            │          │ id (uuid, pk)        │
│ email (unique)           │          │ name (unique)        │
│ phone_number (unique)    │          │ description          │
│ hashed_password (null-   │          └─────────┬────────────┘
│   able → federated-only) │                    │
│ is_active                │                    │
│ federated_provider/_sub  │                    │
└────────────┬─────────────┘                    │
             │                                  │
             │   tuvl_system_iam_user_roles     │   tuvl_system_iam_role_scopes
             │   ┌───────────────────────┐      │   ┌────────────────────────┐
             └──►│ user_id (pk, fk)      │      └──►│ role_id (fk, cascade)  │
                 │ role_id (pk, fk)      │          │ scope_name             │
                 └───────────────────────┘          └────────────────────────┘
```

- A **user** authenticates with a bcrypt `hashed_password`, a federated identity, or both. `email` is the canonical identity across both paths.
- A **role** is a named bundle of scope strings (e.g. `hr_manager` → `requisition:write`, `candidate:read`). Role names become the token's `groups` claim.
- At mint time, the engine collects the user's roles and the **distinct union** of all their scopes; both are embedded in the token. Deleting a role cascades its scope rows.

Users and roles are administered under `/auth/admin/users` and `/auth/admin/roles` (full CRUD plus role assignment/revocation), all guarded by `iam:admin`. Role scope sets are replaced atomically via `PATCH /auth/admin/roles/{id}/scopes`.

---

## 7. Authorization Surfaces

| Surface | Route(s) | Requirement |
|---|---|---|
| Model CRUD | `/models/{model}/…` | Off unless the model opts in per operation with `spec.api.crud: [list, read, create, update, delete]`. Then `{model.lower()}:read` / `:write` / `:delete` by convention; overridable per model via `spec.access.{read,write,delete}_scope`, optionally pinned to IAM groups via `spec.access.{read,write,delete}_groups`; absent entirely when `spec.api.expose_model_crud: false` |
| Workflow triggers | `spec.trigger.http.path`, and `POST /api/workflows/{name}/runs` | `metadata.required_scope` and/or `metadata.required_group`; a valid bearer token is required even when both are omitted, unless the workflow opts into `spec.trigger.http.public: true` |
| MCP export | `/mcp` | `mcp:connect`, plus each exported workflow's own trigger gates (`tools/list` hides what the caller can't run) |
| Runs | `GET /api/runs*` | signed in: your own runs (whoever started a run may follow it); `iam:admin` reads all |
| Run control | `POST /api/runs/{id}/pause\|resume\|abort\|steer` | `runs:control` |
| Approvals | `GET /api/approvals`, `POST /api/approvals/{id}`, `POST /api/runs/{id}/human/{agent}` | `approvals:decide` plus the agent's or tool's `required_group`; never the triggering principal when a group is required ([engines](engines.md#human-a-person-fills-a-form)) |
| Engine admin | `/admin/*` (scope catalogue, model versions, …) | `iam:admin` |
| IAM admin | `/auth/admin/*` (users, roles, federation) | `iam:admin` |
| Artifact API | `/api/artifacts` | `artifacts:read` to list/read, `artifacts:write` to upload (a new version row per upload, never in-place) |
| Dev & Insight | `/dev/*`, `/insight` | dev mode only; the dev session key (§9) |

Notes:

- **CRUD scope derivation** happens in `tuvl/core/api/crud_router.py`: `read_scope = spec.access.read_scope or f"{model_name.lower()}:read"`, and likewise for write (POST/PATCH) and delete. Every CRUD route also runs `bind_principal_context`, so all requests are authenticated even for read.
- **CRUD group pinning.** `spec.access.read_groups` / `write_groups` / `delete_groups` (a list, or a bare group name) add an IAM-group requirement alongside the scope — both must be satisfied; `require_access(scope, groups)` in `access.py` is the dependency factory each CRUD route depends on, wrapping `authorize_token`. A tier left undeclared cascades from the next-more-privileged tier (`write_groups` falls back to `read_groups`, `delete_groups` falls back to `write_groups`), so pinning only `read_groups` never leaves mutations open to a wider audience than reads. Declaring no groups at all is scope-only. `iam:admin` bypasses every scope and group.
- **Workflow gates are metadata-only.** Exactly `metadata.required_scope` and `metadata.required_group` gate a workflow — nothing inside `agents:` changes trigger auth. `required_group` names an IAM role; membership in that single group is required (alongside the scope, when both are declared). `iam:admin` bypasses both.
- **Trigger default-deny.** Every way of starting a run — the workflow's HTTP route, `POST /api/workflows/{name}/runs`, and MCP export — requires a valid bearer token by default, even for a workflow that declares neither `required_scope` nor `required_group`. Anonymous access is an explicit opt-in via `spec.trigger.http.public: true`; `tuvl validate` rejects `public: true` combined with a declared scope or group (V019), and a declared scope/group always wins over `public` at runtime, so a contradictory config fails closed. The decision is one function, `run_denial` in `tuvl/api/auth.py` (over `WorkflowAuthPolicy`, `tuvl/core/auth/workflow_policy.py`), shared by the REST and MCP paths so the enforcement sites can't drift.
- **Scope discovery.** `GET /admin/scopes` (itself `iam:admin`) returns every enforceable scope grouped by source — `crud` (per model, honoring overrides), `workflows` (per `required_scope`), and `system` (`["iam:admin"]`) — so an admin composing roles doesn't have to grep YAML. The same response's `crud_api_enabled` field reports whether the CRUD kill switch (below) is currently on.
- **Run visibility.** A principal sees and operates only its own runs unless it holds `iam:admin`; a run it can't read is a 404, not a 403, so ids don't leak.
- **Artifact uploads are prompt-level trust.** A prompt/steering artifact carries instruction-level authority once a workflow references it, so `artifacts:write` belongs to the same principals who may edit workflows; `iam:admin` bypasses both artifact scopes.
- **CRUD kill switch.** The entire auto-generated `/models/*` surface can be turned off project-wide with `spec.api.expose_model_crud: false` in `.tuvl/system.yaml` (`SystemConfig`), overridable by the `TUVL_EXPOSE_MODEL_CRUD` env var (env wins over YAML), and editable from the Insight Settings page's API Access section in dev mode. When disabled, `build_crud_routers` (`tuvl/core/api/crud_router.py`) never mounts the CRUD routers — the routes are absent, not merely scope-denied — leaving only hand-authored `Workflow` triggers exposed. Takes effect on restart.

Example workflow gate:

```yaml
kind: Workflow
metadata:
  name: screen_candidate
  required_scope: requisition:write
  required_group: hr_manager
```

---

## 8. One IAM Implementation

The entire IAM surface — bootstrap, login, session lifecycle, user/role CRUD, federation-provider admin — has exactly one implementation: `tuvl/core/auth/iam_service.py`. It takes a plain DB session and Python inputs and returns plain results or raises a typed `IamError`; it has no FastAPI types. `tuvl/core/auth/router.py` is a **thin REST adapter** over it, mapping each `IamError` subclass to an HTTP status (`NotFoundError` → 404, `ConflictError` → 409, `AuthenticationError` → 401, `ValidationError` → 400, `ParseError` → 422, `PersistenceError` → 500). The security-relevant behavior lives in the service: login timing-equalization (`iam_service.login` always performs one bcrypt comparison, even against `DUMMY_PW_HASH` for a missing/passwordless account), the federation-provider path sanitizer (`iam_service.safe_federation_path`), and threadpool-offloaded bcrypt and file I/O. The Insight UI calls these REST endpoints directly.

---

## 9. Dev Mode vs Production

### Dev mode (`tuvl dev`)

`tuvl dev` generates a per-session security key of the form `XXXX-XXXX-XXXX-XXXX` (uppercase letters + digits, ~83 bits of entropy; `tuvl/cli/session.py`) and persists it to `.tuvl/.dev-session` with mode 0600. The CLI exports `TUVL_DEV_MODE=true`, `TUVL_ALLOW_DEV_AUTH=true`, and `TUVL_DEV_API_KEY=<key>` into the engine process.

What the dev key does:

- **Gates the dev surfaces.** `/dev/*` (the dev API Insight uses: project files, validate, codegen, specs, dev runs, breakpoints, judges) is mounted only in dev mode and guarded by a pure-ASGI middleware (`tuvl/core/dev/middleware.py`) enforcing an IP allowlist plus the dev key as bearer token. An unset key fails closed — an empty key never authenticates. Breakpoints, step mode and context editing exist only there.
- **Acts as a superuser credential.** In dev mode, `verify_token` accepts the raw dev key — compared with `hmac.compare_digest` — and map it to synthetic claims for user `dev`, group `dev`, scope `iam:admin`, minted once per process. The same key therefore works for `/dev/*`, `/auth/admin/*`, and every scoped route.
- **`--auto-login`** sets `TUVL_DEV_AUTO_LOGIN=1`, which makes the Insight index page embed the key in a `<meta name="tuvl-dev-key">` tag so the UI skips its security screen. Off by default.

Two boot-time refusals in `tuvl/core/auth/access.py` keep the shortcut out of production: `TUVL_DEV_MODE=true` with `TUVL_ENV=production` aborts the process unconditionally, and outside production dev mode still requires the explicit `TUVL_ALLOW_DEV_AUTH=true` acknowledgement (which `tuvl dev` sets for you).

When no signing key is configured in dev mode, an ephemeral Ed25519 key pair is generated at startup — tokens become invalid on restart, and a warning says so.

### Production (`tuvl run`)

Production runs with dev mode off: no dev key, no `/dev` or Insight execution surfaces, signed tokens only. `TUVL_SIGNING_KEY` **must** be set — `TuvlKeyManager` (`tuvl/core/auth/keys.py`) fails closed with a `RuntimeError` pointing at `tuvl keys generate` when the variable is missing outside dev mode.

---

## 10. Key Management

The engine signs and verifies all tokens with a single Ed25519 key pair, managed by `TuvlKeyManager`:

```bash
# Print a fresh key (and the .env line to paste)
tuvl keys generate

# Or write it straight into the project .env (mode 0600); --force to overwrite
tuvl keys generate --write
```

- `TUVL_SIGNING_KEY` holds the hex-encoded 32-byte Ed25519 private key (64 hex chars), loaded once per process. `TUVL_BISCUIT_PRIVATE_KEY` (the 1.x name, same format) is still read, with a warning to rename it — the key itself carries over unchanged.
- The derived public key verifies every incoming token; the private key is used only for minting.
- Keep the key secret and stable: rotating it invalidates every outstanding token (all users re-login). There is no multi-key rollover; rotation is a hard cutover.
- Because verification is offline, any process holding only the *public* key can verify tuvl tokens — fetch it from `GET /auth/jwks.json` (the `kid` in each token's header names it). Such a verifier can't see logouts or ended sessions.

---

## 11. Failure Modes

| Status | Meaning | Typical causes |
|---|---|---|
| 401 / `UNAUTHENTICATED` | Not (or no longer) authenticated | missing/malformed token; bad signature; expired token; `alg` other than `EdDSA`; wrong issuer; revoked (blacklisted) token; token issued before its user's sessions were ended; token missing `sub`/`iat` or carrying a malformed claim; wrong password (generic message, timing-equalised) |
| 403 / `PERMISSION_DENIED` | Authenticated but not allowed | missing required scope or group; federated email domain not in `allowed_domains`; unverified-email auto-link refusal; disabled account at federated login |
| 404 / `NOT_FOUND` | Resource absent — or deliberately indistinguishable from absent | unknown user/role id in admin CRUD; unknown workflow; another principal's or tenant's run (existence not leaked) |
| 409 / `ALREADY_EXISTS` | State conflict | bootstrap after first user exists; duplicate user email or role name |
| 501 | Provider declared but unusable | federation provider missing `client_id`/`client_secret` |

Rules of thumb: 401 always carries `WWW-Authenticate: Bearer` and means "re-authenticate"; 403 means "re-authenticating as yourself won't help"; login failures never distinguish unknown-user from wrong-password.

---

## 12. Reading the Code

| Module | Role |
|---|---|
| `tuvl/core/auth/access.py` | Verification pipeline (`decode_token`, `verify_bearer_token`), `authorize_token`, `require_scope` / `require_groups`, `bind_principal_context`, dev-mode shortcut, boot-time dev-mode sentinel |
| `tuvl/core/auth/token.py` | `mint_token` — claim layout, expiry, name validation |
| `tuvl/core/auth/keys.py` | `TuvlKeyManager` — `TUVL_SIGNING_KEY` loading (legacy name too), key id + JWK, ephemeral dev key, fail-closed production check |
| `tuvl/core/auth/models.py` | The four `tuvl_system_iam_*` tables |
| `tuvl/core/auth/crypto.py` | bcrypt hashing/verification + threadpool wrappers |
| `tuvl/core/auth/blacklist.py` | Token revocation store (Redis / in-process) |
| `tuvl/core/auth/iam_service.py` | Transport-neutral IAM service — the one implementation of bootstrap, login, refresh/logout, user & role CRUD, and federation-provider admin; `router.py` is a thin adapter over it |
| `tuvl/core/auth/router.py` | `/auth` REST surface: bootstrap, login, me/refresh/logout, user & role admin, OAuth federation flow — a thin adapter over `iam_service.py` |
| `tuvl/core/auth/federation_loader.py` | `kind: FederationProvider` YAML loader and registry |
| `tuvl/core/api/crud_router.py` | CRUD scope/group derivation, enforcement, `build_crud_routers` kill-switch gate |
| `tuvl/core/auth/workflow_policy.py` | `WorkflowAuthPolicy` — default-deny and `public` |
| `tuvl/api/auth.py` | `current_user`, `run_denial` / `trigger_user` — the one trigger-permission decision for REST and MCP; `require` for scoped routes |
| `tuvl/api/runs.py` | Runs, events, control and approvals routes; run visibility |
| `tuvl/api/mcp_export.py` | `/mcp` — `mcp:connect` gate and per-tool workflow gates |
| `tuvl/runtime/human.py` | Approval authorisation (group, no self-approval) |
| `tuvl/core/system_config.py` | `expose_model_crud` — `.tuvl/system.yaml` / `TUVL_EXPOSE_MODEL_CRUD` resolution for the CRUD kill switch |
| `tuvl/core/api/admin_router.py` | `/admin/*` guard, `GET /admin/scopes` |
| `tuvl/core/dev/middleware.py` | `/dev/*` dev-key + IP-allowlist gate |
| `tuvl/cli/session.py`, `tuvl/cli/commands/dev.py` | Dev security key generation and session file |
| `tuvl/cli/commands/keys.py` | `tuvl keys generate` |
| `tests/core/test_auth_router.py`, `tests/core/test_iam_rest.py` | Behavior pins for login, token lifecycle, and IAM admin |
