---
name: ui-auth
description: "Sign users in from a UI with @tuvl/client: TuvlAuth password and OAuth login, set-password links, where the token lives, refresh before expiry, current user and scopes, 401 and 403."
---
# Sign-in and tokens

```ts
import { TuvlAuth, isTokenExpired, readOAuthToken, readToken } from "@tuvl/client";
const auth = new TuvlAuth({ baseUrl: BASE });
```
| Call | Use |
|---|---|
| `auth.loginWithPassword(email, password)` | `{ access_token }` |
| `auth.getOAuthLoginUrl("google" \| "github" \| "microsoft")` | send the browser there; the provider returns to `TUVL_OAUTH_UI_REDIRECT_URL#token=…` |
| `readOAuthToken({ clear: true })` | on that landing page: the token from the URL fragment, then removed from the address bar and history; `undefined` when the page was not reached by a sign-in |
| `auth.setPassword(linkToken, password)` | the page a set-password link opens (`#/set-password?token=…`); `400` for a used, expired or wrong link, `422` for a password that breaks the rules |
| `auth.getMe(token)` | `{ user_id, groups, scopes }` from the server; fails for a revoked token or ended session |
| `auth.refresh(token)` | a new `{ access_token }` |
| `auth.logout(token)` | ends the session |
| `auth.bootstrap(...)` | never part of a product UI: a production server does not mount it (the first admin comes from `TUVL_BOOTSTRAP_ADMIN_*`) |

## Flow
1. Sign in → `client.setToken(access_token)`.
2. On the OAuth landing page call `readOAuthToken({ clear: true })` before anything else runs (the token is in the fragment, never the query string).
   Open the provider's start URL in the same browser that lands: the server binds the sign-in to that browser and refuses a callback without it.
3. Load `getMe` once to learn `groups` and `scopes`; use them to hide screens and buttons. The server still answers `403` when they were wrong.
4. Refresh **before** the token expires: `if (isTokenExpired(token)) token = (await auth.refresh(token)).access_token; client.setToken(token)`.
   `isTokenExpired` is true when the token is expired or within 30 s of it. Refresh needs a token that is still valid and revokes the old one,
   so store the new token everywhere at once and never refresh twice in parallel.
5. Sign out → `auth.logout(token)`, then drop the token from memory and storage.

## Status handling
- `401`: no token, or it expired or was revoked (logout, refresh, a deactivated user) → show sign-in. An expired token cannot be refreshed; do not retry in a loop.
- `403`: signed in but not allowed (missing scope or group) → a no-access state, do not retry, do not sign out.
- `429` on sign-in: too many wrong passwords for that login or address. Show a wait message using `Retry-After` (seconds); do not retry automatically.
- `422` when setting a password: it breaks the rule (8 characters to 72 bytes). Say so next to the field. Emails are case-insensitive.

## Where the token lives
- In memory (a module variable or state) or in an `HttpOnly` cookie set by your own backend. Not in `localStorage`, the repository, the bundle, logs or analytics.
- `readToken(token)` decodes the claims (`sub`, `groups`, `scopes`, `exp`) for display only. It does **not** verify the signature; never make an access decision from it.
- Server-rendered or shared code serving several users: build a client per request, or pass `{ token }` per call. One shared client holds one user's token.
- A workflow with `trigger.http.public: true` (its spec file's Auth row says so) needs no token; a client without one is fine there. Every other route needs a verified token.
- The token travels in the `Authorization` header, so a browser on another origin needs the backend to allow that origin first (`tuvl-ui`, `spec.api.cors_origins`). Without it, sign-in itself fails in the browser.
