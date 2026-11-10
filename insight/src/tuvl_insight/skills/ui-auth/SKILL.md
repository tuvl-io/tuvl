---
name: ui-auth
description: "Sign users in from a UI with @tuvl/client: TuvlAuth password and OAuth login, where the token lives, refresh before expiry, current user and scopes, 401 and 403."
---
# Sign-in and tokens

```ts
import { TuvlAuth, isTokenExpired, readToken } from "@tuvl/client";
const auth = new TuvlAuth({ baseUrl: BASE });
```
| Call | Use |
|---|---|
| `auth.loginWithPassword(email, password)` | `{ access_token }` |
| `auth.getOAuthLoginUrl("google" \| "github" \| "microsoft")` | send the browser there; the provider returns to `TUVL_OAUTH_UI_REDIRECT_URL` with `?token=` |
| `auth.getMe(token)` | `{ user_id, groups, scopes }` from the server; fails for a revoked token or ended session |
| `auth.refresh(token)` | a new `{ access_token }` |
| `auth.logout(token)` | ends the session |
| `auth.bootstrap(...)` | one-time creation of the first admin; never part of a product UI (a second call is `409`) |

## Flow
1. Sign in → `client.setToken(access_token)`.
2. On the OAuth landing page read `token` from the query string, then remove it from the address bar (`history.replaceState`) before anything else runs.
3. Load `getMe` once to learn `groups` and `scopes`; use them to hide screens and buttons. The server still answers `403` when they were wrong.
4. Refresh **before** the token expires: `if (isTokenExpired(token)) token = (await auth.refresh(token)).access_token; client.setToken(token)`.
   `isTokenExpired` is true when the token is expired or within 30 s of it. Refresh needs a token that is still valid and revokes the old one,
   so store the new token everywhere at once and never refresh twice in parallel.
5. Sign out → `auth.logout(token)`, then drop the token from memory and storage.

## Status handling
- `401`: no token, or it expired or was revoked (logout, refresh, a deactivated user) → show sign-in. An expired token cannot be refreshed; do not retry in a loop.
- `403`: signed in but not allowed (missing scope or group) → a no-access state, do not retry, do not sign out.

## Where the token lives
- In memory (a module variable or state) or in an `HttpOnly` cookie set by your own backend. Not in `localStorage`, the repository, the bundle, logs or analytics.
- `readToken(token)` decodes the claims (`sub`, `groups`, `scopes`, `exp`) for display only. It does **not** verify the signature; never make an access decision from it.
- Server-rendered or shared code serving several users: build a client per request, or pass `{ token }` per call. One shared client holds one user's token.
- A workflow with `trigger.http.public: true` (its spec file's Auth row says so) needs no token; a client without one is fine there. Every other route needs a verified token.
- The token travels in the `Authorization` header, so a browser on another origin needs the backend to allow that origin first (`tuvl-ui`, `spec.api.cors_origins`). Without it, sign-in itself fails in the browser.
