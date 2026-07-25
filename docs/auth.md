# Authentication & Authorization

tuvl's auth stack is built on **Biscuit tokens** — cryptographically signed, offline-verifiable bearer tokens whose claims are Datalog facts — backed by a small relational IAM (users, roles, scopes) and enforced by one shared guard across REST and gRPC. Authentication answers *who is calling* (a verified `user()` fact); authorization answers *what they may do* (`scope()` and `group()` facts checked against per-resource requirements).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Token Anatomy](#2-token-anatomy)
3. [The Verification Pipeline](#3-the-verification-pipeline)
4. [Login & Identity](#4-login--identity)
5. [Federation Providers](#5-federation-providers)
6. [The IAM Model](#6-the-iam-model)
7. [Authorization Surfaces](#7-authorization-surfaces)
8. [REST & gRPC Parity](#8-rest--grpc-parity)
9. [Dev Mode vs Production](#9-dev-mode-vs-production)
10. [Key Management](#10-key-management)
11. [Failure Modes](#11-failure-modes)
12. [Reading the Code](#12-reading-the-code)

---

## 1. Overview

Two planes, one token:

- **Authentication (who).** A caller presents a Biscuit token in `Authorization: Bearer <token>`. The engine verifies its Ed25519 signature against the process key pair and extracts the `user()` fact. No database lookup is needed to verify a token — verification is fully offline.
- **Authorization (what).** The token also carries `scope()` facts (fine-grained permissions such as `candidate:read`) and `group()` facts (the caller's IAM role names). Each protected surface declares a required scope and/or group; enforcement is a set-membership check on the verified facts.

```
                 login (password / OAuth)                request
                          │                                 │
                          ▼                                 ▼
   IAM tables ──► mint_biscuit_token ──► Biscuit ──► verify signature (Ed25519)
   (roles →        user()/group()/        b64            │
    scopes)        scope()/exp()                          ▼
                                                enforce_token_security
                                                (TTL + attenuated checks)
                                                          │
                                                          ▼
                                                  authorize_token
                                             (required scope AND group,
                                              iam:admin bypasses both)
```

Key properties:

- **Stateless verification.** The IAM database is consulted only at mint time. A token is a snapshot of the user's roles and scopes; role changes take effect on the next mint (login or refresh).
- **Revocation is the one stateful exception.** Logout and refresh push the token's SHA-256 hash onto a Redis-backed blacklist checked on every REST request.
- **One guard, every transport.** `authorize_token` and `enforce_token_security` in `tuvl/core/auth/biscuit_auth.py` are the single source of truth for both the FastAPI dependencies and the gRPC servicers.

---

## 2. Token Anatomy

Tokens are minted by `mint_biscuit_token` in `tuvl/core/auth/token.py`. The authority block carries these Datalog facts:

```
user("3f6c…-uuid");                     // who the token belongs to
group("hr_manager");                    // one fact per IAM role
scope("candidate:read");                // one fact per granted scope
exp(1750000000);                        // integer UNIX expiry (optional)
check if time($t), $t <= 2026-07-05T…;  // cryptographically enforced TTL
tenant("acme");                         // optional, multi-tenant only
```

Details that matter:

- **TTL is embedded twice.** When `ttl_seconds` is set (default `TUVL_TOKEN_TTL_SECONDS`, 86400), the token carries both an `exp()` integer fact (cheap introspection) and a `check if time($t), $t <= <exp>` rule. The check is part of the signed token — no allow-all authorizer policy can override it.
- **Injection-safe minting.** All fact values are injected through Biscuit's parameterized API (`{key}` substitution), never string interpolation. Scope and group names are additionally validated against `^[\w:.\-/]+$` before minting; the same pattern is enforced by `require_scope` / `require_groups` on the consuming side.
- **Attenuation.** Because Biscuits are offline-attenuable, a holder can append blocks with additional `check` clauses (e.g. a shorter deadline) to derive a strictly weaker token. Appended checks are evaluated on every request by `enforce_token_security` — attenuation cannot widen access, only narrow it.
- The result is a URL-safe Base64 string, sent as a standard bearer token.

---

## 3. The Verification Pipeline

Every REST request passes through the dependency chain in `tuvl/core/auth/biscuit_auth.py`:

```
Authorization header
    │
    ▼
verify_token / verify_bearer_token
    ├── 401 if no token
    ├── dev-mode shortcut: dev key → synthetic iam:admin Biscuit   (§9)
    ├── Biscuit.from_base64(token, public_key)   ← signature check, offline
    ├── token_blacklist.is_revoked(token)        ← Redis / in-process
    └── enforce_token_security(biscuit)          ← TTL + attenuated checks
            │
            ▼
get_current_user(biscuit) → TokenUser(user_id, groups, scopes, tenant_id)
            │
            ▼
authorize_token(biscuit, required_scope=…, required_groups=…)
```

**`enforce_token_security`** exists because token-side checks are *not* evaluated by signature parsing alone. It builds an authorizer with `set_time()` (providing `time($now)` for the TTL rule) and a permissive `allow if true` policy, then calls `authorize()` — which runs every `check` clause embedded in the token, including attenuated ones. It then reads the `exp()` fact as a Python-side fallback. It is transport-neutral and MUST run on every verified Biscuit: the REST path calls it inside `verify_bearer_token`, the gRPC servicers call it inside `_verify_biscuit`. Malformed Datalog fails closed (`TokenExpiredError`).

**`authorize_token`** is the single scope/group decision point:

- Missing `user()` fact or a failed token check → `TokenUnauthorizedError` (401 / `UNAUTHENTICATED`).
- When both a scope and a group are required, the token must satisfy **both** (AND, not OR). Within `required_groups`, membership in any one listed group suffices.
- **`iam:admin` is the superuser bypass** — a token carrying the `iam:admin` scope satisfies every scope and group requirement and returns immediately.
- Otherwise a missing scope/group → `TokenForbiddenError` (403 / `PERMISSION_DENIED`).

`require_scope("x:y")` and `require_groups([...])` are FastAPI dependency factories that wrap `authorize_token` and translate its errors to `HTTPException`s. They validate the scope/group names against the safe-character pattern at router-construction time.

**`bind_principal_context`** is an async dependency layered on `get_current_user` that writes the verified `user_id` (and `tenant_id`, when the token carries a `tenant()` fact) into request-scoped ContextVars, so structlog and OTel spans annotate every record with the caller identity. Tenant binding is how opt-in multi-tenant deployments scope database sessions; single-tenant deployments can ignore it — the ContextVar simply stays unset.

Behavior is pinned by `tests/core/test_auth_router.py` and `tests/core/test_grpc_token_expiry.py` (fresh token accepted, expired token rejected on both transports, servicers call `enforce_token_security`).

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

The callback returns the same kind of Biscuit as password login, or redirects to `TUVL_OAUTH_UI_REDIRECT_URL` with the token as a query param when configured. `TUVL_OAUTH_BASE_URL` sets the redirect-URI origin registered with the IdP.

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
- A **role** is a named bundle of scope strings (e.g. `hr_manager` → `requisition:write`, `candidate:read`). Role names become the token's `group()` facts.
- At mint time, the engine collects the user's roles and the **distinct union** of all their scopes; both are embedded in the token. Deleting a role cascades its scope rows.

Users and roles are administered under `/auth/admin/users` and `/auth/admin/roles` (full CRUD plus role assignment/revocation), all guarded by `iam:admin`. Role scope sets are replaced atomically via `PATCH /auth/admin/roles/{id}/scopes`.

---

## 7. Authorization Surfaces

| Surface | Route(s) | Requirement |
|---|---|---|
| Model CRUD | `/models/{model}/…` | `{model.lower()}:read` / `:write` / `:delete` by convention; overridable per model via `spec.access.{read,write,delete}_scope` in the model YAML |
| Workflow triggers | `trigger.path` from the workflow YAML | `metadata.required_scope` and/or `metadata.required_group`; **omit both and the route is public (unauthenticated)** |
| Versioned execution | `/{api_version}/run/{workflow}` | same `metadata.required_scope` / `required_group`, enforced in-handler after the versioned config is resolved |
| Engine admin | `/admin/*` (workflow toggle, fork, scope catalogue, …) | `iam:admin` |
| IAM admin | `/auth/admin/*` (users, roles, federation) | `iam:admin` |
| Operator API | `/api/agents/*` | `agent:observe` to read, `agent:control` to act |
| Artifact API | `/api/artifacts` | `artifacts:read` to list/read, `artifacts:write` to upload (a new version row per upload, never in-place) |
| HITL resume | `/…/resume` | owner / `auth.required_group` / `iam:admin` — see `human-in-the-loop.md` §5 |
| Dev & Insight | `/dev/*`, `/api/insight/*` | dev-mode security key, never Biscuit-based (§9) |

Notes:

- **CRUD scope derivation** happens in `tuvl/core/api/crud_router.py`: `read_scope = spec.access.read_scope or f"{model_name.lower()}:read"`, and likewise for write (POST/PATCH) and delete. Every CRUD route also runs `bind_principal_context`, so all requests are authenticated even for read.
- **Workflow gates are metadata-only.** `_build_route_deps` in `tuvl/core/api/manager.py` reads exactly `metadata.required_scope` and `metadata.required_group` from the workflow YAML — nothing inside `steps:` changes route auth. `required_group` names an IAM role; membership in that single group is required (alongside the scope, when both are declared).
- **Scope discovery.** `GET /admin/scopes` (itself `iam:admin`) returns every enforceable scope grouped by source — `crud` (per model, honoring overrides), `workflows` (per `required_scope`), and `system` (`["iam:admin"]`) — so an admin composing roles doesn't have to grep YAML.
- The operator API additionally scopes runs by tenant and returns 404 for foreign-tenant run ids rather than leaking existence.
- **Artifact uploads are prompt-level trust.** A prompt/steering artifact carries instruction-level authority once a workflow references it, so `artifacts:write` belongs to the same principals who may edit workflows; `iam:admin` bypasses both artifact scopes.

Example workflow gate:

```yaml
kind: Workflow
metadata:
  name: screen_candidate
  required_scope: requisition:write
  required_group: hr_manager
```

---

## 8. REST & gRPC Parity

The gRPC-Web surface (used by the Insight UI) enforces the same contract with the same primitives:

- `tuvl/core/grpc/iam_servicer.py` mirrors the entire `/auth` REST surface (`Bootstrap`, `Login`, `GetMe`, `RefreshToken`, `Logout`, user/role CRUD, role assignment, federation-provider management). Its `_verify_biscuit` performs signature validation **and** calls `enforce_token_security`, matching the REST `verify_token` contract; `_require_admin` then checks for the `iam:admin` scope.
- `tuvl/core/grpc/servicer.py` (`ExecutionServicer.RunWorkflow`) authenticates the token from call metadata, then enforces the workflow's `metadata.required_scope` / `required_group` through the shared `authorize_token` — the same function the REST route dependencies use.
- Error mapping is mechanical: `TokenUnauthorizedError` / expired / invalid → `UNAUTHENTICATED`; missing scope or group → `PERMISSION_DENIED`.
- Every IAM RPC is wrapped in the `@_managed` decorator, which scopes database-session cleanup to the single call: any session opened during the handler is deterministically closed when the call returns, raises, or aborts.

Password verification on the gRPC login path routes through the same threadpool bcrypt wrappers as REST.

---

## 9. Dev Mode vs Production

### Dev mode (`tuvl dev`)

`tuvl dev` generates a per-session security key of the form `XXXX-XXXX-XXXX-XXXX` (uppercase letters + digits, ~83 bits of entropy; `tuvl/cli/session.py`) and persists it to `.tuvl/.dev-session` with mode 0600. The CLI exports `TUVL_DEV_MODE=true`, `TUVL_ALLOW_DEV_AUTH=true`, and `TUVL_DEV_API_KEY=<key>` into the engine process.

What the dev key does:

- **Gates the dev surfaces.** `/dev/*` is guarded by a pure-ASGI middleware (`tuvl/core/dev/middleware.py`) enforcing an IP allowlist plus the dev key as bearer token; the Insight execution endpoints (`/api/insight/*`) and their gRPC twins are gated by the same key and refuse to run at all outside dev mode. An unset key fails closed — an empty key never authenticates.
- **Acts as a superuser credential.** In dev mode, `verify_token` (and the gRPC equivalents) accept the raw dev key — compared with `hmac.compare_digest` — and map it to a synthetic Biscuit for user `dev`, group `dev`, scope `iam:admin`, minted once per process. The same key therefore works for `/dev/*`, `/auth/admin/*`, and every scoped route.
- **`--auto-login`** sets `TUVL_DEV_AUTO_LOGIN=1`, which makes the Insight index page embed the key in a `<meta name="tuvl-dev-key">` tag so the UI skips its security screen. Off by default.

Two boot-time refusals in `tuvl/core/auth/biscuit_auth.py` keep the shortcut out of production: `TUVL_DEV_MODE=true` with `TUVL_ENV=production` aborts the process unconditionally, and outside production dev mode still requires the explicit `TUVL_ALLOW_DEV_AUTH=true` acknowledgement (which `tuvl dev` sets for you).

When no signing key is configured in dev mode, an ephemeral Ed25519 key pair is generated at startup — tokens become invalid on restart, and a warning says so.

### Production (`tuvl run`)

Production runs with dev mode off: no dev key, no `/dev` or Insight execution surfaces, Biscuit tokens only. `TUVL_BISCUIT_PRIVATE_KEY` **must** be set — `TuvlKeyManager` (`tuvl/core/auth/keys.py`) fails closed with a `RuntimeError` pointing at `tuvl keys generate` when the variable is missing outside dev mode.

---

## 10. Key Management

The engine signs and verifies all tokens with a single Ed25519 key pair, managed by `TuvlKeyManager`:

```bash
# Print a fresh key (and the .env line to paste)
tuvl keys generate

# Or write it straight into the project .env (mode 0600); --force to overwrite
tuvl keys generate --write
```

- `TUVL_BISCUIT_PRIVATE_KEY` holds the hex-encoded 32-byte private key (64 hex chars), loaded once per process.
- The derived public key verifies every incoming token; the private key is used only for minting.
- Keep the key secret and stable: rotating it invalidates every outstanding token (all users re-login). There is no multi-key rollover; rotation is a hard cutover.
- Because verification is offline, any process holding only the *public* key can verify tuvl tokens — useful for sidecars or downstream services.

---

## 11. Failure Modes

| Status | Meaning | Typical causes |
|---|---|---|
| 401 / `UNAUTHENTICATED` | Not (or no longer) authenticated | missing/malformed token; bad signature; expired TTL or failed attenuated check; revoked (blacklisted) token; token missing its `user()` fact; wrong password (generic message, timing-equalised) |
| 403 / `PERMISSION_DENIED` | Authenticated but not allowed | missing required scope or group; federated email domain not in `allowed_domains`; unverified-email auto-link refusal; disabled account at federated login |
| 404 / `NOT_FOUND` | Resource absent — or deliberately indistinguishable from absent | unknown user/role id in admin CRUD; unknown workflow/version; foreign-tenant agent run (existence not leaked) |
| 409 / `ALREADY_EXISTS` | State conflict | bootstrap after first user exists; duplicate user email or role name |
| 501 | Provider declared but unusable | federation provider missing `client_id`/`client_secret` |

Rules of thumb: 401 always carries `WWW-Authenticate: Bearer` and means "re-authenticate"; 403 means "re-authenticating as yourself won't help"; login failures never distinguish unknown-user from wrong-password.

---

## 12. Reading the Code

| Module | Role |
|---|---|
| `tuvl/core/auth/biscuit_auth.py` | Verification pipeline, `enforce_token_security`, `authorize_token`, `require_scope` / `require_groups`, `bind_principal_context`, dev-mode shortcut, boot-time dev-mode sentinel |
| `tuvl/core/auth/token.py` | `mint_biscuit_token` — fact layout, TTL embedding, injection-safe parameters |
| `tuvl/core/auth/keys.py` | `TuvlKeyManager` — `TUVL_BISCUIT_PRIVATE_KEY` loading, ephemeral dev key, fail-closed production check |
| `tuvl/core/auth/models.py` | The four `tuvl_system_iam_*` tables |
| `tuvl/core/auth/crypto.py` | bcrypt hashing/verification + threadpool wrappers |
| `tuvl/core/auth/blacklist.py` | Token revocation store (Redis / in-process) |
| `tuvl/core/auth/router.py` | `/auth` REST surface: bootstrap, login, me/refresh/logout, user & role admin, OAuth federation flow |
| `tuvl/core/auth/federation_loader.py` | `kind: FederationProvider` YAML loader and registry |
| `tuvl/core/api/crud_router.py` | CRUD scope derivation and enforcement |
| `tuvl/core/api/manager.py` | `_build_route_deps` — workflow `metadata.required_scope` / `required_group` gates |
| `tuvl/core/api/execution_router.py` | Versioned run route auth, `/admin/*` guard, `GET /admin/scopes` |
| `tuvl/core/api/orchestrator_router.py` | Operator API (`agent:observe` / `agent:control`) |
| `tuvl/core/grpc/iam_servicer.py` | gRPC IAM surface, `_verify_biscuit`, `@_managed` session lifecycle |
| `tuvl/core/grpc/servicer.py` | gRPC workflow execution auth (shared `authorize_token`) |
| `tuvl/core/dev/middleware.py` | `/dev/*` dev-key + IP-allowlist gate |
| `tuvl/cli/session.py`, `tuvl/cli/commands/dev.py` | Dev security key generation and session file |
| `tuvl/cli/commands/keys.py` | `tuvl keys generate` |
| `tests/core/test_auth_router.py`, `tests/core/test_grpc_token_expiry.py` | Behavior pins for login lookup and cross-transport expiry |
