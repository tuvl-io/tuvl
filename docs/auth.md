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
- **Revocation is the one stateful exception.** Logout and refresh refuse the token from then on, and narrowing a user's access ends their sessions (below). Both are kept in Redis when a Redis datasource is deployed and in Postgres otherwise, and checked on every authenticated request.
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
    ├── token_blacklist.refusal(token, claims)   ← Redis, or Postgres when no Redis is deployed
    │     ├── revoked?                           ← jti (Postgres) / SHA-256 of the token (Redis)
    │     └── issued before the user's sessions ended? ← iat vs sessions ended at
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

All identity endpoints live under `/auth`: in `tuvl/core/auth/router.py`, except federated login and federation-provider admin, which are in `tuvl/core/auth/federation.py`.

### Bootstrap

`POST /auth/bootstrap` creates the first administrator. It is only available while `tuvl_system_iam_users` is empty (409 afterwards) and requires no token. It creates a `superadmin` role carrying `iam:admin` (or reuses a seeded one), assigns it to the new user, and returns a token.

It belongs to the `iam` API group, which a production server does not mount (see [Production IAM](#production-iam)); on a production database the first administrator comes from the environment instead.

### Password login

`POST /auth/token` is a standard OAuth2 password-grant form. The `username` field is the user's email address (a phone number also matches — lookup is `email OR phone_number`).

- **bcrypt off the event loop.** Hashing and verification run at cost factor 12 (~200–300 ms of pure CPU) and are always dispatched through `run_in_threadpool` (`aget_password_hash` / `averify_password` in `tuvl/core/auth/crypto.py`), so login never stalls the ASGI event loop. Passwords are truncated to bcrypt's 72-byte limit before hashing, matching bcrypt's own semantics.
- **Login-timing equalisation.** When the account is missing, inactive, or passwordless, the handler still verifies the submitted password against a throwaway hash (`_DUMMY_PW_HASH`), so response timing cannot be used to enumerate which emails exist. All failure cases return the same generic 401.
- **Login throttle.** Password guesses are slowed per login and per address. After **5 wrong passwords** a login waits 2, 4, 8 ... seconds (up to 15 minutes) before its next attempt is judged; meanwhile `POST /auth/token` answers `429` with `Retry-After`, and even the right password is refused (no password is checked while a login waits). The count is kept under a hash of the lower-cased login in `tuvl_system_iam_login_attempts`, so an unknown login waits exactly like a known one and the answer never says which exist; a successful sign-in deletes it, and a login with no failure for a day is forgotten. `tuvl iam` is throttled the same way. One **client address** may make `TUVL_LOGIN_IP_FAILURES` (30, `0` turns it off) failed attempts in 15 minutes, then waits out the window; that counter lives in the process, so each worker counts its own, and behind a proxy the address is the proxy's unless `--forwarded-allow-ips` names it (see `docs/runtime.md`). Failures are audited as `auth.login.failed` (a hash of the login, never the login or the password). A waiting user is released with `tuvl iam users unlock <email>`; `reset-admin` also clears the wait of the account it recovers.

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

Revocation and session end (`tuvl/core/auth/blacklist.py`) live in one of two places, chosen by the deployment:

- **Redis**, when the project has a `type: redis` datasource and it is reachable: keys `tuvl:blacklist:<sha256 of the token>` and `tuvl:sessions-ended:<user id>`, with an expiry matching the token lifetime (none for a token that has no `exp`). A check is one round trip. Keep Redis persistent, or a restart forgets what it held.
- **Postgres**, on the primary datasource, when there is no Redis datasource: tables `tuvl_system_iam_revoked_tokens` (the token's `jti`, removed once the token would have expired) and `tuvl_system_iam_session_ends` (one row per user, which outlives a deleted user). A write is made in the transaction of the change that caused it, so they commit or roll back together. A worker keeps its own answer for `TUVL_SESSION_CACHE_SECONDS` (default 5; `0` asks the database on every request); a change made in another worker or by `tuvl iam` is seen within that time, a change in the same process at once.

Either way a revocation or session end made by one worker, by `tuvl iam` in a process of its own, or before a restart, is seen by every other worker. `tuvl iam` selects the store the same way the server does; if a Redis datasource is configured but cannot be reached from where the command runs it refuses to change access, rather than record the change where the server does not look. If the server cannot reach Redis at boot it logs `redis.unreachable.fallback_postgres` and uses Postgres (`TUVL_REQUIRE_REDIS=true` refuses to boot instead).

When the store cannot be reached the token is refused with `503`, never let in. A process with no primary datasource at all has nothing to ask: the check is skipped and `auth.session_store.none` is logged once. With `TUVL_TOKEN_TTL_SECONDS=0` tokens never expire and so does a revocation; boot logs an error in production.

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
  # allowed_users: [octocat]   # github only: login handles, case-insensitive
  # default_role: member
  # pkce: true                 # S256; on by default for google and microsoft, off for github
```

`${VAR}` references are resolved from the environment at load time (`${VAR:default}` supports defaults); a provider whose variables are unset is skipped with a warning. Built-in URL defaults exist for Google, GitHub, and Microsoft. A legacy fallback reads `TUVL_OAUTH_<PROVIDER>_CLIENT_ID` / `_CLIENT_SECRET` settings when no YAML exists for a built-in provider.

The flow is OAuth 2.0 authorization-code:

1. `GET /auth/oauth/{provider}/start` — stores a one-time `state` (with a PKCE verifier where the provider takes one), sets the browser cookie below, redirects to the IdP.
2. `GET /auth/oauth/{provider}/callback` — consumes the state, checks the cookie, exchanges the code (sending the PKCE `code_verifier`), fetches the user profile, and upserts an IAM user keyed by `(federated_provider, federated_sub)`.

**The callback belongs to the browser that started it.** `start` sets `tuvl_oauth_state`, a short-lived (10 minutes) `HttpOnly`, `SameSite=Lax` cookie (`Secure` when `TUVL_OAUTH_BASE_URL` is https, scoped to `/auth/oauth`) holding a hash of the state, and the callback refuses a state whose cookie is missing or different (`400`, and the state is spent). Without it a link someone else started could be finished in a victim's browser, signing the victim in as the attacker. `TUVL_OAUTH_STATE_COOKIE=false` turns the check off for a client that cannot keep cookies between the two requests (it is also what a device flow does without). **PKCE** (S256) is on by default for Google and Microsoft and off for GitHub unless the file says `pkce: true`; set `pkce: false` for a custom provider that rejects it.

**Where the state lives.** In Redis when a `type: redis` datasource is deployed and reachable, otherwise in `tuvl_system_oauth_states` in the primary database; either way a callback may land on a different worker or replica than the start. A state and a device handle are single-use (one `GETDEL`-style step in Redis, one `DELETE ... RETURNING` in Postgres), expire after 10 minutes, and expired rows are removed whenever a new one is stored.

**Microsoft.** The built-in `userinfo_url` is the OIDC endpoint (`https://graph.microsoft.com/oidc/userinfo`), which carries `email` (grant the `email` scope). If an older file points `userinfo_url` at Graph's `/v1.0/me`, the email is taken from `mail`, or from `userPrincipalName` when it contains an `@`. Microsoft does not assert `email_verified`, so a Microsoft sign-in is never auto-linked to an existing password account: the person signs in with their credentials first, as for any unverified provider.

Account-linking rules in the callback:

- **`email_verified` is required for auto-link.** If an account with the same email already exists but the IdP did not assert the email as verified, the callback returns 403 instead of linking — otherwise an attacker registering an unverified IdP identity under a victim's email could take over the account. For GitHub, only addresses from the verified `/user/emails` list are trusted.
- `allowed_domains`, when set, rejects any email outside the list (403). `allowed_users` (GitHub) rejects any login handle outside the list (403); a file that sets it for another provider is **refused at load** (`auth.federation.provider_refused` is logged), because no other provider has a login handle and it would turn everyone away.
- New federated users are created passwordless; `default_role`, when set and existing in the IAM database, is auto-assigned so first-time users don't arrive with zero permissions.

The callback returns the same kind of token as password login, or redirects to `TUVL_OAUTH_UI_REDIRECT_URL` with the token in the URL **fragment** (`#token=…`, so it never reaches a server log) when configured. `TUVL_OAUTH_BASE_URL` sets the redirect-URI origin registered with the IdP.

**Device flow (GitHub).** A CLI or other headless client signs in without a browser callback:

1. `POST /auth/oauth/github/device/start` returns `handle`, `user_code`, `verification_uri`, `interval`, `expires_in`. GitHub's device code stays on the server.
2. The user opens the URI and enters the code.
3. The client polls `POST /auth/oauth/github/device/poll {"handle": …}` every `interval` seconds: `428` while pending, `200` with a token once approved (same account-linking rules as above), `400` when denied or expired. A handle is single-use.

Tick **Enable Device Flow** on the GitHub OAuth app. `tuvl register project` and Insight's Register dialog use this flow ([project](project.md)).

Provider YAMLs can be managed at runtime via `/auth/admin/federation` (list/get/put/delete, `iam:admin` required). A `PUT` is checked against the document's shape (`kind`, `metadata.name` equal to the URL name, known fields only, `spec.provider` one of `google | github | microsoft | custom`, `client_id` and `client_secret` present, endpoint URLs `https://` (plain `http://` only to `localhost`), `allowed_users` for GitHub only) and answers `400` with what is wrong, writing nothing. `client_secret` is redacted (`***`) in read responses, and a `PUT` that sends `***` back keeps the secret that is stored, so an admin screen can edit a provider without ever seeing its secret. The files are local to the replica that served the request: with several replicas, make a change in the image or the mounted config, not through this API. Federation logs name the user by id, never by email.

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

**Rules every IAM path applies** (REST, `tuvl iam`, the seed): a **password** is 8 characters at least and 72 bytes at most (bcrypt ignores the rest), wherever it is set: admin-created users, set-password links, the bootstrap and seeded administrators, `reset-admin`; a **role name** and a **scope** use letters, digits and `_ : . - /` only (the token minter refuses anything else, so `orders:*` or a name with a space is refused with `422` where it is written, and a `RoleSeed` with one fails `tuvl lint project`); an **email** is stored trimmed and lower case and compared without regard to case, with a unique index on `lower(email)` built at boot (users stored earlier that differ only by case log `iam.email.duplicates` and the index waits until they are merged; sign-in still works). A scope or role name stored before these rules that a token cannot carry is left out of that user's token and logged as `auth.token.invalid_entry_skipped`, so it cannot lock out every member of its role; remove it with `tuvl iam roles revoke`. The first administrator is created under the same advisory lock as the seed, so two `POST /auth/bootstrap` calls (or a bootstrap and a booting replica) make one.

Users and roles are administered under `/auth/admin/users` and `/auth/admin/roles` (full CRUD plus role assignment/revocation), all guarded by `iam:admin`. Role scope sets are replaced atomically via `PATCH /auth/admin/roles/{id}/scopes`.

**Set-password links.** An admin makes a one-time link token for a user with `POST /auth/admin/users/{id}/invite` (`iam:admin`; optional `{"ttl_hours": 72}`, 1 to 720; the answer is `{token, expires_at}`). The token is shown once: only its SHA-256 is stored. A user created without a `password` has no way to sign in until they use it. The user then calls the public `POST /auth/password/set` with `{token, password}` (8 to 72 bytes): `204` on success; `400` for a token that is wrong, already used or expired; `422` for a password that breaks the rules (the token is kept). The token works once, a newer link replaces the older unused one, an inactive user cannot be given one, and setting the password ends the sessions the user already held. Put the token in a link in your own UI (for example `https://app.example.com/#/set-password?token=...`) and send it by email.

---

## 7. Authorization Surfaces

| Surface | Route(s) | Requirement |
|---|---|---|
| Model CRUD | `/models/{model}/…` | Off unless the model opts in per operation with `spec.api.crud: [list, read, create, update, delete]`. Then `{model.lower()}:read` / `:write` / `:delete` by convention; overridable per model via `spec.access.{read,write,delete}_scope`, optionally pinned to IAM groups via `spec.access.{read,write,delete}_groups`; absent entirely when `spec.api.expose_model_crud: false` |
| Workflow triggers | `spec.trigger.http.path`, and `POST /api/workflows/{name}/runs` | `metadata.required_scope` and/or `metadata.required_group`; a valid bearer token is required even when both are omitted, unless the workflow opts into `spec.trigger.http.public: true` |
| MCP export | `/mcp` | `mcp:connect`, plus each exported workflow's own trigger gates (`tools/list` hides what the caller can't run) |
| Runs | `GET /api/runs*` | signed in: your own runs (whoever started a run may follow it); `runs:read` reads (and lists) **any** run of the caller's tenant, which is what a webhook receiver or a dashboard holds; `iam:admin` reads all |
| Run control | `POST /api/runs/{id}/pause\|resume\|abort\|steer` | `runs:control` |
| Approvals | `GET /api/approvals`, `POST /api/approvals/{id}`, `POST /api/runs/{id}/human/{agent}` | `approvals:decide` plus the agent's or tool's `required_group`; never the triggering principal when a group is required ([engines](engines.md#human-a-person-fills-a-form)) |
| Engine admin | `/admin/*` (scope catalogue, model versions, …) | `iam:admin` |
| IAM admin | `/auth/admin/*` (users, roles, federation) | `iam:admin` |
| Artifact API | `/api/artifacts` | `artifacts:read` to list/read, `artifacts:write` to upload (a new version row per upload, never in-place) |
| Dev & Insight | `/dev/*`, `/insight` | dev mode only; the dev session key (§9) |

Notes:

- **CRUD scope derivation** happens in `tuvl/core/api/crud_router.py`: `read_scope = spec.access.read_scope or f"{model_name.lower()}:read"`, and likewise for write (POST/PATCH) and delete. Every CRUD route also runs `bind_principal_context`, so all requests are authenticated even for read.
- **`secure` fields and `<model>:read_secure`.** **`?include=` needs the related model's read access.** Embedding a relation returns that model's records, so the caller needs what the related model asks of a reader (its `:read` scope, or `spec.access.read_scope`, and its `read_groups` when it pins any; `iam:admin` passes), or the request is `403` naming the scope. A model field declared `secure: true` is left out of every CRUD response (list, read, create and update, and the records an `?include=` embeds) unless the token holds `<model>:read_secure` (the model name in lower case, for the model whose field it is, so an embedded record needs its own model's scope) or `iam:admin`. `<model>:read` alone returns the record without them; `<model>:read_secure` alone grants nothing. They are optional in the read schema (OpenAPI and `@tuvl/client` types say so). The scope is listed in `tuvl iam check` and Insight for models that have secure fields. Filtering on a secure field is refused for everyone. See `docs/observability.md` §10 for the rest of the rule.
- **CRUD group pinning.** `spec.access.read_groups` / `write_groups` / `delete_groups` (a list, or a bare group name) add an IAM-group requirement alongside the scope — both must be satisfied; `require_access(scope, groups)` in `access.py` is the dependency factory each CRUD route depends on, wrapping `authorize_token`. A tier left undeclared cascades from the next-more-privileged tier (`write_groups` falls back to `read_groups`, `delete_groups` falls back to `write_groups`), so pinning only `read_groups` never leaves mutations open to a wider audience than reads. Declaring no groups at all is scope-only. `iam:admin` bypasses every scope and group.
- **Workflow gates are metadata-only.** Exactly `metadata.required_scope` and `metadata.required_group` gate a workflow — nothing inside `agents:` changes trigger auth. `required_group` names an IAM role; membership in that single group is required (alongside the scope, when both are declared). `iam:admin` bypasses both.
- **Trigger default-deny.** Every way of starting a run — the workflow's HTTP route, `POST /api/workflows/{name}/runs`, and MCP export — requires a valid bearer token by default, even for a workflow that declares neither `required_scope` nor `required_group`. Anonymous access is an explicit opt-in via `spec.trigger.http.public: true`; `tuvl lint project` rejects `public: true` combined with a declared scope or group (V019), and a declared scope/group always wins over `public` at runtime, so a contradictory config fails closed. The decision is one function, `run_denial` in `tuvl/api/auth.py` (over `WorkflowAuthPolicy`, `tuvl/core/auth/workflow_policy.py`), shared by the REST and MCP paths so the enforcement sites can't drift.
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

- **Gates the dev surfaces.** `/dev/*` (the dev API Insight uses: project files, validate, codegen, specs, dev runs, breakpoints, judges) is mounted only in dev mode and guarded by a pure-ASGI middleware (`tuvl/core/dev/middleware.py`) enforcing an IP allowlist plus the dev key as bearer token. `--allow-host` (`TUVL_ALLOWED_HOSTS`) applies to every route of the dev server, not only `/dev/*`. An unset key fails closed — an empty key never authenticates. Breakpoints, step mode and context editing exist only there.
- **Acts as a superuser credential.** In dev mode, `verify_token` accepts the raw dev key — compared with `hmac.compare_digest` — and map it to synthetic claims for user `dev`, group `dev`, scope `iam:admin`, minted once per process. The same key therefore works for `/dev/*`, `/auth/admin/*`, and every scoped route.
- **`--auto-login`** sets `TUVL_DEV_AUTO_LOGIN=1`, which makes the Insight index page embed the key in a `<meta name="tuvl-dev-key">` tag so the UI skips its security screen. Off by default. The key is embedded only when the request comes from this machine (a loopback client address) and is addressed as `localhost` or by a loopback address; a browser on another machine, or a page that reaches the server through a DNS name, pastes the key instead.

Two boot-time refusals in `tuvl/core/auth/access.py` keep the shortcut out of production: `TUVL_DEV_MODE=true` with `TUVL_ENV=production` aborts the process unconditionally, and outside production dev mode still requires the explicit `TUVL_ALLOW_DEV_AUTH=true` acknowledgement (which `tuvl dev` sets for you).

When no signing key is configured in dev mode, an ephemeral Ed25519 key pair is generated at startup — tokens become invalid on restart, and a warning says so.

### Production (`tuvl serve app`)

Production runs with dev mode off: no dev key, no `/dev` or Insight execution surfaces, signed tokens only. `TUVL_SIGNING_KEY` **must** be set — `TuvlKeyManager` (`tuvl/core/auth/keys.py`) fails closed with a `RuntimeError` pointing at `tuvl generate key` when the variable is missing outside dev mode.

Users and roles are database rows, and `tuvl ship` carries none of them. A production database starts empty and has no Insight, so IAM is set up in three steps: a seed on the first boot, then `tuvl iam` from inside the deployment.

### Production IAM

**1. Roles: `kind: RoleSeed`.** Keep the roles a fresh environment should start with in the project:

```yaml
kind: RoleSeed
spec:
  roles:
    - name: superadmin
      description: Full platform administrator
      scopes: [iam:admin]
    - name: recruiter
      description: Reads candidates and interviews
      scopes: [candidate:read, interview:read]
```

`tuvl lint project` checks it (V044). Model the roles in Insight in development, then write them out with `tuvl iam export --out iam/roles.yaml` (users and passwords are never exported).

**2. The first administrator: environment.** Set `TUVL_BOOTSTRAP_ADMIN_EMAIL` and `TUVL_BOOTSTRAP_ADMIN_PASSWORD` (or `TUVL_BOOTSTRAP_ADMIN_PASSWORD_FILE` with a mounted secret). Put the password in the cluster Secret, not in the chart values.

**3. Once.** On boot, in one transaction under an advisory lock (several replicas boot together), tuvl seeds the roles and the administrator, then writes an `iam_seeded` marker to `tuvl_system_registry`. The seed runs **once in the life of a database**:

- a database that already has a user or a role is left alone (the marker is written and the seed ignored), so a restored or migrated database is never overwritten;
- the marker, not emptiness, is what stops a second seed, so deleting every role later does not bring the seed back;
- a boot with nothing to seed writes no marker, so a later boot that carries a seed still applies.

Roles and users are changed afterwards with `tuvl iam`. Editing the `RoleSeed` file later changes nothing in a running database; `tuvl iam check` tells you when the project and the database drift.

**`tuvl iam`.** Production mounts no IAM administration over HTTP: `/auth/bootstrap` and `/auth/admin/*` are the `iam` API group, which is off unless you expose it. Run the command where the database is reachable:

```bash
kubectl exec -it deploy/<app> -- tuvl iam roles list
```

It asks for an administrator's email and password (prompted, `--password-stdin`, or `TUVL_IAM_EMAIL` / `TUVL_IAM_PASSWORD`), then works on the primary datasource directly. The credentials say who made a change; they are not a network boundary, because whoever can exec into the pod already holds the database credentials.

| Command | Does |
|---|---|
| `roles list` · `create` · `delete` · `enable` · `disable` | The roles. A disabled role keeps its members and scopes but grants nothing. `delete` refuses a role that still has members unless `--force`. |
| `users create <email> [--no-password]` · `users invite <email> [--url ...{token}...] [--hours N]` | A user with no password and a one-time set-password link for them (see *Set-password links*). `invite` prints only the link, or the bare token without `--url`, so it can be captured and sent to the user yourself; it works once and a newer one replaces an older unused one. |
| `roles grant <role> <scope>` · `revoke` | One scope at a time. Revoking signs the holders out. |
| `roles add-user <role> <email>` · `remove-user` | Who holds the role. |
| `users list` · `create` · `enable` · `disable` · `unlock` | The users. `create` prompts for the password (or `TUVL_IAM_NEW_PASSWORD`). `unlock <email>` lets a user who is made to wait after failed sign-ins try again now. |
| `scopes list` | The scopes this project enforces and what each guards. |
| `check` | Is every scope the project enforces granted by an active role? Exits 1 if not. Run it after a release that adds a scope. |
| `reset-admin <email>` | Break-glass: makes the user an active administrator with a new password and restores the `superadmin` role. Asks for no login, so only use it when no administrator can sign in. |
| `export` | Developer toolchain only: the live roles as a `RoleSeed`. |

The service refuses a change that would leave no active administrator (a user holding an active role with `iam:admin`): disabling or deleting the last such role, revoking `iam:admin` from it, removing its last member, or disabling or deleting the last such user. A database that never had an administrator is not locked. `tuvl iam` changes appear in the logs as `auth.iam.cli` events with the acting administrator.

To reach the REST admin API in production anyway, add `iam` to `spec.api.expose` / `TUVL_API_EXPOSE`. Nothing in tuvl needs it.

### Role management from your own screen (`roles`)

Most deployments need one runtime IAM task: someone on the team decides who holds which role. The `roles` API group does that and nothing else, so you can build a screen for it without exposing `iam`. It is **not mounted in production** until you ask for it:

```yaml
# .tuvl/system.yaml
kind: SystemConfig
spec:
  api:
    expose: [auth, roles]      # the list replaces the default, so keep auth
```

or `TUVL_API_EXPOSE=auth,roles`. Callers need the scope **`roles:manage`** (`iam:admin` also passes). Give it to the role your administrators use, in the `RoleSeed`:

```yaml
- name: role-managers
  scopes: [roles:manage, reports:read, reports:write]
```

| Route | Does |
|---|---|
| `GET /auth/roles` | Every role: name, description, `is_active`, the scopes it grants (read-only), member count |
| `GET /auth/roles/{id}/members` | A role's members, paged (`limit`, `cursor`) |
| `GET /auth/roles/candidates?q=` | Find users to add: id, email, names, `is_active`; paged |
| `PUT /auth/roles/{id}/members/{user_id}` | Add a user to the role (repeating it is harmless) |
| `DELETE /auth/roles/{id}/members/{user_id}` | Remove a user from the role |
| `PATCH /auth/roles/{id}` with `{"is_active": false}` | Disable the role: it keeps its members and scopes but grants nothing, and its members' sessions end. `true` enables it again |

**What it does not do.** Creating or deleting roles, changing the scopes a role grants, adding a scope to an endpoint, creating users and federation are development-time work: `RoleSeed`, the endpoint's `scope`, and `tuvl iam`. None of them is reachable over HTTP in production.

**A manager cannot hand out access they do not hold.** Whoever can change membership could otherwise add themselves to a stronger role. So a caller without `iam:admin` may change a role only if every scope the role grants is one they hold themselves, and never a role that carries `iam:admin` or `roles:manage`: those change hands through an administrator or `tuvl iam`. A refused change answers `403` and names the reason. The last-administrator rule above applies here too (`409`). Every change is logged as an `auth.iam.roles` event with the acting user, the role and the user.

From TypeScript, `@tuvl/client` wraps these routes as `client.roles()`.

---

## 10. Key Management

The engine signs and verifies all tokens with a single Ed25519 key pair, managed by `TuvlKeyManager`:

```bash
# Print a fresh key (and the .env line to paste)
tuvl generate key

# Or write it straight into the project .env (mode 0600); --force to overwrite
tuvl generate key --write
```

- `TUVL_SIGNING_KEY` holds the hex-encoded 32-byte Ed25519 private key (64 hex chars), loaded once per process.
- The derived public key verifies every incoming token; the private key is used only for minting.
- Keep the key secret. Changing `TUVL_SIGNING_KEY` on its own invalidates every outstanding token (all users sign in again); rotate with `TUVL_SIGNING_KEY_PREVIOUS` instead (below).
- Because verification is offline, any process holding only the *public* key can verify tuvl tokens — fetch them from `GET /auth/jwks.json` (the `kid` in each token's header names the one to use). Such a verifier can't see logouts or ended sessions.

**Rotating the signing key without signing anyone out.** `TUVL_SIGNING_KEY_PREVIOUS` takes one or more retired private keys (64 hex characters each, comma-separated). They sign nothing; their public keys verify tokens already issued and are published at `/auth/jwks.json` after the current key. Because a rolling deploy runs old and new pods side by side, rotate in three steps:

1. Make a key (`tuvl generate key`) and deploy with it in `TUVL_SIGNING_KEY_PREVIOUS` while `TUVL_SIGNING_KEY` still holds the current one. Every pod can now verify the new key; none signs with it yet.
2. Deploy again with the two swapped: `TUVL_SIGNING_KEY` is the new key, `TUVL_SIGNING_KEY_PREVIOUS` the old one. New tokens carry the new `kid`; tokens issued earlier still verify.
3. After the longest token lifetime has passed (`TUVL_TOKEN_TTL_SECONDS`, 24 hours by default), remove `TUVL_SIGNING_KEY_PREVIOUS`. A token still signed by the retired key is refused from then on.

A value that is not 64 hex characters stops the boot naming the variable. Revocations and session ends do not depend on the key: a token revoked before the rotation stays revoked.

---

## 11. Failure Modes

| Status | Meaning | Typical causes |
|---|---|---|
| 401 / `UNAUTHENTICATED` | Not (or no longer) authenticated | missing/malformed token; bad signature; expired token; `alg` other than `EdDSA`; wrong issuer; revoked (blacklisted) token; token issued before its user's sessions were ended; token missing `sub`/`iat` or carrying a malformed claim; wrong password (generic message, timing-equalised) |
| 403 / `PERMISSION_DENIED` | Authenticated but not allowed | missing required scope or group; federated email domain not in `allowed_domains`; unverified-email auto-link refusal; disabled account at federated login |
| 404 / `NOT_FOUND` | Resource absent — or deliberately indistinguishable from absent | unknown user/role id in admin CRUD; unknown workflow; another principal's or tenant's run (existence not leaked) |
| 409 / `ALREADY_EXISTS` | State conflict | bootstrap after first user exists; duplicate user email (in any case) or role name |
| 429 | Too many failed sign-ins | `POST /auth/token` for a login (or an address) that has to wait; `Retry-After` says how long |
| 422 | Input the IAM rules refuse | a password under 8 characters or over 72 bytes; a role name or scope a token cannot carry |
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
| `tuvl/core/auth/blacklist.py` | Token revocation and session-end store (Redis, or Postgres when no Redis is deployed) |
| `tuvl/core/auth/iam_service.py` | Transport-neutral IAM service — the one implementation of bootstrap, login, refresh/logout, user & role CRUD, and federation-provider admin; `router.py` is a thin adapter over it |
| `tuvl/core/auth/router.py` | `/auth` REST surface: bootstrap, login, me/refresh/logout, user & role admin — a thin adapter over `iam_service.py` |
| `tuvl/core/auth/federation.py` | Federated login (OAuth authorization-code and device flows) and the `/auth/admin/federation` provider admin |
| `tuvl/core/auth/seed.py` | `kind: RoleSeed` and the once-only first-boot seed (roles + `TUVL_BOOTSTRAP_ADMIN_*` administrator) |
| `tuvl/cli/commands/iam.py` | `tuvl iam`: the production IAM command line |
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
| `tuvl/cli/commands/keys.py` | `tuvl generate key` |
| `tests/core/test_auth_router.py`, `tests/core/test_iam_rest.py` | Behavior pins for login, token lifecycle, and IAM admin |
