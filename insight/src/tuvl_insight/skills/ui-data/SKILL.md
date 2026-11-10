---
name: ui-data
description: "Model data, file uploads and webhooks from a UI with @tuvl/client: crud() list, get, create, update, delete, multipart uploads for file inputs, and why webhooks belong on a server."
---
# Data, files and webhooks

## Model data: `client.crud`
Only models documented in `ui/specs/models.md` have routes, and only the operations they list (`spec.api.crud`).
```ts
const tickets = client.crud<TicketRead, TicketCreate>("ticket");   // the name is lower-cased in the path
const page = await tickets.list({ limit: 20, offset: 0, filters: { status: "open" }, include: ["customer"] });
const one = await tickets.get(id, { include: ["customer"] });
await tickets.create({ subject, body });
await tickets.update(id, { status: "closed" });       // PATCH: only the fields you pass
await tickets.delete(id);
```
- `limit` is 1–1000 (default 100). Filters are `field: value` strings, never built into the URL by hand.
- Scopes: `{model}:read` (list, get), `{model}:write` (create, update), `{model}:delete`; a model can also require groups (`403` otherwise).
- The create form sends only fields marked "Sent on create"; the server sets the rest. Fields marked `secure` are PII: never log, echo, cache or put them in analytics.
- Changes that must be journaled or contract-checked go through a **workflow**, not CRUD. Use CRUD to show and lightly edit rows.
- Refused calls reject `TuvlApiError` (`status`, `detail`).

## Files
A workflow whose trigger declares `http.upload` takes `file` / `list[file]` inputs. Pass a `File` or `Blob`; the client sends `multipart/form-data`.
```ts
await client.execute("intake", { invoice: fileInput.files[0], note: "March" });
```
- Never base64 a file into JSON and never build the file reference yourself; the server makes it. The answer is the same as for any run (`ui-run`).
- Limits are in the workflow's spec file (files per request, bytes each, accepted types). Check them in the UI first, and still handle the refusals:
  `413` too large, `415` type not accepted, `400` too many files, `422` a value that is not a file.
- Show the file's `name` and `size`; the file's own address is not returned.

## Webhooks belong on a server
A webhook tells your backend that a run reached a stage; it carries the run id and a position, no data. The receiving side needs the secret, so it is never
the browser or a phone app.
```ts
import { webhookHandler } from "@tuvl/client";

export const POST = webhookHandler({
  secrets: [process.env.OPS_WEBHOOK_SECRET!],          // the old secret too while rotating
  onNotification: async (n) => { if (!(await seen(n.id))) await push(await client.run(n.run_id).get()); },
});
```
- Verify the raw body with `verifyWebhook(rawBody, header["tuvl-signature"], secrets)` or `webhookHandler`; never a re-serialised body. Delivery is at least once: de-duplicate on `n.id`.
- Pull the run with your own service token (scope `runs:read`), then push only the run id to a device. The app pulls with the user's own token.
- Without a server, follow the run with `handle.events()` instead (`ui-run`).
