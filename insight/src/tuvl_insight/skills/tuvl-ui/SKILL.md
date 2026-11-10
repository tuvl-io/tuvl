---
name: tuvl-ui
description: "Build the user interface of a tuvl backend with @tuvl/client: read ui/specs, set up the typed client and a same-origin proxy, build a screen per workflow, handle every status. Start here for any UI task."
---
# Building a UI on a tuvl backend

The UI is a separate app (any framework) that talks to the backend only through `@tuvl/client`. Sibling skills go deeper:
`ui-run` (start and follow a run), `ui-approve` (people waiting), `ui-auth` (sign-in, tokens), `ui-data` (CRUD, uploads, webhooks).

## 1. Read what the backend offers
- `tuvl generate ui-spec` writes `ui/specs/`: `README.md` (setup and rules), one file per workflow, `models.md`, `ui-contract.json`.
  `tuvl generate ui-spec --check` exits 1 when the files are stale. Write your own notes only below the notes marker.
- A workflow has a file only once its tasks are done. **No file means not ready: do not build a screen for it** and do not guess its fields.
- Each file lists the route, auth, input fields, every status it can return and real examples. The examples are your fixtures;
  `ui-contract.json` is the same facts as data (use it to mock the backend in UI tests).

## 2. Set up once
```bash
npm install @tuvl/client
npx tuvl-client generate --url http://localhost:8885 --out src/tuvl.generated.ts   # or --file openapi.json
```
- A production server serves no OpenAPI by default. Generate the types from `tuvl dev` or a staging server, never by hand.
  Regenerate when a workflow contract changes.
- One module owns the client:
```ts
import { TuvlAuth, TuvlClient } from "@tuvl/client";
import type { Workflows } from "./tuvl.generated";
export const auth = new TuvlAuth({ baseUrl: BASE });
export const client = new TuvlClient<Workflows>({ baseUrl: BASE, token });   // client.setToken(t) after sign-in or refresh
```

## 3. Make the backend reachable from the browser
A browser app on another origin (a Vite dev server, a CDN) is blocked unless the backend allows it. `ui/specs/README.md`, under "Backend surface", says which case applies. Pick one:
- **Allow the UI's origin** (no proxy): the backend owner adds it to `.tuvl/system.yaml`, or sets `TUVL_CORS_ORIGINS`, and restarts.
  ```yaml
  spec:
    api:
      cors_origins: [https://app.example.com, http://localhost:5173]   # scheme://host[:port], no trailing slash
  ```
  Nothing is allowed by default, not even localhost. Do not ask for `*` unless the API is meant for any website. CORS is not authentication.
- **Share one origin**: serve the UI and the backend together and proxy `/api`, `/auth` and `/models` to the backend; then `BASE = window.location.origin`.
  ```ts
  // vite.config.ts, development
  import { defineConfig } from "vite";
  const backend = "http://localhost:8885";
  export default defineConfig({ server: { proxy: { "/api": backend, "/auth": backend, "/models": backend } } });
  ```
  In production use the same three prefixes in the reverse proxy and turn response buffering off for `/api/runs/*/events` (server-sent events).

Never work around a CORS error in the client (`no-cors`, a public CORS proxy). A server-side or mobile client is not affected.

## 4. One screen per workflow
Form from the input table (name, type, required) → `client.execute` → show a state for **every** row of "What can come back":
success body, an error end, `202` (follow the run), `401` sign in, `403` no access, `409` try later, `422` field errors beside their inputs.
Details: `ui-run`. A `file` input needs the upload path: `ui-data`.

## Rules
1. Call only what the spec files document. If something is missing the backend does not have it: ask for it in the spec, do not invent a route or field.
2. Use `@tuvl/client` for runs, events, approvals, CRUD and sign-in; no hand-written `fetch` to tuvl.
3. Authorization is the backend's. Hide actions the token's scopes do not allow, and still expect a `403`.
4. A token, a signing key or a `secure` field never goes into the repo, the bundle, logs or analytics.
5. Do not call `/api/_system`, `/admin` or `/api/artifacts` unless the spec's "Backend surface" lists them; production answers `404`.
6. The methods of 1.x (`executeVersioned`, `resumeWorkflow`, `getManifest`, `listWorkflows`) do not exist. If a method is not in these skills, check the type definitions in `node_modules/@tuvl/client` before using it.

## Done when
- [ ] Every ready workflow has a screen, and each status in its table has a visible state.
- [ ] A `202` is followed to its end and survives a page refresh (keep the `run_id`).
- [ ] Sign-in, expiry and `403` are handled; types are generated, not written by hand.
- [ ] Tests run against the spec's examples; one pass against `tuvl dev` with a real token.
