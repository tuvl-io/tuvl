# Storage

Workflows read and write files — invoices, attachments, exports, model inputs — through **named
stores**. A store is declared once in `storage/*.yaml` and points at S3 (or any S3-compatible
service), Azure Blob Storage, Google Cloud Storage, or a directory on the engine host. tuvl talks to
them with its own HTTP client; no cloud SDK is installed.

## Declare a store

```yaml title="storage/invoices.yaml"
kind: Storage
version: v1
metadata:
  name: invoices                     # the name workflows use
spec:
  provider: s3                       # s3 | azure | gcs | local
  bucket: acme-invoices
  region: eu-west-1
  access_key: ${AWS_ACCESS_KEY_ID}
  secret_key: ${AWS_SECRET_ACCESS_KEY}
  prefix: invoices/                  # every key lives under this prefix
  max_object_bytes: 16777216         # per put/get through the tool (default 16 MiB, ceiling 64 MiB)
```

| Field | Applies to | Description |
|---|---|---|
| `provider` | all | `s3`, `azure`, `gcs` or `local` |
| `prefix` | all | Keys are confined under it; `list` never shows keys outside it |
| `readonly` | all | Refuses `put`, `copy`, `delete` and `presign` with `PUT` |
| `max_object_bytes` | all | Largest object the tool writes or reads (default 16 MiB, at most 64 MiB) |
| `presign_max_seconds` | all | Longest lifetime of a presigned URL (default 3600) |
| `timeout` | all | Request timeout in seconds (default 30) |
| `endpoint` | s3, azure, gcs | Service URL. `https`, or `http` for `localhost` only |
| `bucket`, `region`, `path_style` | s3, gcs | `path_style` defaults to `true` with an `endpoint` |
| `access_key`, `secret_key`, `session_token` | s3, gcs | `${ENV}` references. GCS uses them as HMAC keys |
| `role_arn` | s3 | Role for web identity (EKS); also read from `AWS_ROLE_ARN` |
| `account`, `container` | azure | Storage account and container |
| `account_key`, `sas_token` | azure | `${ENV}` references; with neither, a managed identity is used |
| `credentials`, `access_token`, `project` | gcs | Service-account JSON (or path to it) or a bearer token, as `${ENV}` references |
| `root` | local | Directory, relative to the project (default `./data/storage`) |

**Secrets are `${ENV_VAR}` references only.** A literal key in the YAML is a validation error.
`${VAR:default}` is accepted; a variable with no value and no default fails when the store is opened.

### Credentials

The first source that yields credentials is used.

| Provider | Order |
|---|---|
| S3 | `access_key`/`secret_key` → `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` → web identity (`AWS_WEB_IDENTITY_TOKEN_FILE`, EKS) → ECS container credentials → EC2 instance role (IMDSv2) |
| Azure | `account_key` → `sas_token` → workload identity (`AZURE_FEDERATED_TOKEN_FILE`, AKS) → managed identity |
| GCS | `access_key`/`secret_key` (HMAC) → `access_token` → service-account JSON → metadata server (GKE, GCE) |

Short-lived tokens are cached and refreshed a minute before they expire. Not built in: AWS shared
config and SSO profiles, assume-role chains, `az login` and `gcloud` user credentials. Run with a
role, a workload identity or a key instead.

### Dialects

The S3 backend serves two vocabularies. Amazon's (`x-amz-*` headers, `AWS4-HMAC-SHA256`, `If-None-Match: *` to refuse an overwrite) is
used for `provider: s3`, which covers AWS, MinIO, R2, Wasabi, Spaces, B2 and Oracle. Google's (`x-goog-*` headers, `GOOG4-HMAC-SHA256`,
`x-goog-if-generation-match: 0`) is used for `provider: gcs` with an HMAC key: Google rejects a request that mixes the two sets of names
and ignores `If-None-Match` on a write, so a create-only write has to be signed and conditioned in Google's own terms. Azure Blob does
not sign this way (SharedKey, SAS or a bearer token) but names its metadata and create-only headers in the same table. The names live in
`src/tuvl/storage/dialects.py`; a service that needs different ones is one more entry there.

### Other services

| Service | Setup |
|---|---|
| MinIO, Cloudflare R2, Wasabi, DigitalOcean Spaces, Backblaze B2 | `provider: s3` with the service's `endpoint` |
| Google Cloud Storage with HMAC keys | `provider: gcs` with `access_key` and `secret_key` (this also enables `presign`) |
| Oracle Cloud Infrastructure Object Storage | `provider: s3` with `endpoint: https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`, `region: <region>` and `path_style: true`; the Customer Secret Key is the `access_key`/`secret_key` pair. It uses Amazon's dialect through OCI's S3 compatibility API (not exercised in this repo's tests) |
| Azurite | `provider: azure`, `endpoint: http://127.0.0.1:10000/devstoreaccount1` |

## Use a store in a workflow

`storage` is the fourth `tool` variant, next to `db`, `http` and `mcp`.

```yaml
agents:
  - id: save_invoice
    engine: tool
    inputs: { ticket_id: str, pdf_b64: str }
    outputs: { blob: json }
    tool:
      use: storage
      store: invoices
      op: put
      key: "{{ ticket_id }}/invoice.pdf"
      body: "{{ pdf_b64 }}"
      encoding: base64
      content_type: application/pdf
    routes: { default: next, conflict: END.exists, error: END.failed }
```

| `op` | Fields | Result (the single output) | Effect |
|---|---|---|---|
| `put` | `key`, `body`, `encoding` (`text`, `base64`, `json`), `content_type`, `metadata`, `if_exists` (`fail` or `overwrite`) | the blob | write |
| `get` | `key`, `as` (`text`, `json`, `base64`), `max_bytes` (default 1 MiB) | the content | read |
| `head` | `key` | the blob | read |
| `list` | `prefix`, `limit` (1–1000), `cursor` | the list of blobs | read |
| `copy` | `key`, `to_key`, `to_store`, `if_exists` | the new blob | write |
| `delete` | `key` | none | destructive |
| `presign` | `key`, `method` (`GET`, `PUT`), `expires_in` | the URL | read (`GET`), write (`PUT`) |

- A **blob** is `{store, key, size, content_type, etag, sha256, modified}`, declared as `json`. The
  key is relative to the store's prefix. `sha256` is computed on `put`. A `local` store keeps it in `.meta/<key>.json` beside the file, so `head`
  and `list` do not read the file again; a file written by an older version is hashed when it is read.
- `key`, `to_key`, `prefix`, `cursor`, `body` and `metadata` are templates (`{{ x }}`).
- With several outputs, `map` fills each from the result, as `http.map` does:
  `map: { text: content, size: blob.size }`. `get` exposes `content` and `blob`; `list` exposes
  `items` and `cursor`; `presign` exposes `url` and `expires_at`.
- **Signals.** `get`, `head` and `copy` emit `not_found`; `put` and `copy` with `if_exists: fail`
  emit `conflict`; `put`, `get` and `copy` may emit `too_large`. The validator requires the first two
  to be routed. `delete` of a missing key succeeds.
- A `put` whose key already holds *identical content* (same SHA-256) succeeds, so a retried step does
  not report a false conflict. Different content with `if_exists: fail` is a `conflict`.
- Object bytes never enter the run journal; a step records the store, op, key, size and SHA-256. A
  `get` result does land in run state as the step's output, so keep `max_bytes` small and move large
  files with `presign`.
- `presign` hands a person or another system a time-limited URL to download or upload a file
  without moving it through a run. Local stores have none.

### In code agents

```python
@agent("archive")
async def archive(inp, ctx):
    store = ctx.storage("invoices")
    blob = await store.put(f"{inp.id}.json", payload, content_type="application/json")
    return {"blob": blob.to_dict()}
```

`ctx.storage(name)` returns the store with `put`, `get`, `head`, `list`, `delete`, `copy` and
`presign`. Inside an agent with `effect: read` the store is read-only. Calls move whole objects up to
`max_object_bytes`.

## Uploads from callers

A trigger can accept `multipart/form-data`. Each `file` input is written to a store and the run gets a
reference to it, so a person can attach a file in one call:

```yaml
spec:
  trigger:
    http:
      path: /api/invoices
      upload: { store: inbox, prefix: in/, max_bytes: 5242880, max_files: 3, accept: [application/pdf, image/*] }
    input:
      invoice: file          # form part "invoice"
      attachments: list[file]?
      note: str?             # other parts are text; a list or object is sent as JSON text
  agents:
    - id: read
      engine: tool
      tool: { use: storage, store: inbox, op: get, key: "{{ invoice.key }}", as: base64 }
      inputs: { invoice: file }
      outputs: { content: str }
```

A `file` value has `store`, `key`, `name`, `size`, `content_type` and `sha256`. `{{ invoice.key }}` works
anywhere an expression does; a code agent reads it with `await ctx.read_file(inp.invoice)`. Agents can
pass it to a child workflow or a human form.

- **Authenticated first.** The caller is checked before any byte is read, so an unauthorised request
  stores nothing.
- **Limits are enforced while the body streams.** `max_bytes` (per file, at most the store's
  `max_object_bytes`) is `413`, `max_files` and part limits are `400`, a content type outside `accept` is
  `415`, a field that is not a file input is `422`. `accept` checks the type the client declares.
- **Keys are generated.** The object lives at `<prefix><random id>/<sanitised name>`; the client's name
  is kept only as `name`. A file named `../../x` cannot leave the prefix.
- **A reference cannot be forged.** A JSON body or query that names a `file` input is `422`: the only
  way to get a reference is to upload through the route (a workflow or schedule can still pass one on).
- **Nothing is left behind by a refused request.** Files stored for a request whose input is then
  refused (`422`) are deleted. Files of a run that starts stay: set a lifecycle rule on the prefix to
  expire them.
- The same body works on `POST /api/workflows/{name}/runs` (one part per input, no `input` wrapper),
  which is what `@tuvl/client` uses when an input is a `File` or `Blob`.
- Files are held in memory while they are stored, so `max_bytes` is also a memory bound. Larger files
  should go straight to the bucket with a `presign` PUT.

MCP export cannot take a file input. Insight's Run form has a file picker and the trigger panel edits
the upload settings.

## Safety

| Concern | Rule |
|---|---|
| Keys | Relative, forward-slashed, at most 1024 bytes, no `..`, no empty or `.` segments, no control characters. A rendered key that breaks this fails the step |
| Prefix | Every key is placed under `prefix`; `list` cannot leave it |
| Local disk | Paths are resolved through symlinks and must stay inside `root` |
| Endpoints | Only from the store file, never from a workflow; `https`, or `http` for `localhost` |
| Secrets | `${ENV}` references only; never in logs, spans, the journal or errors. Presigned URLs are not journaled |
| Read-only | Enforced by the validator and again by the client |
| Destructive | `delete` has the `destructive` effect, so `policy.require_human_before` and loop-tool approval apply |
| Logs | `httpx` request logs have `sig`, `X-Amz-Signature` and token query values redacted |

## Validation

| Code | Check |
|---|---|
| V030 | The store exists |
| V031 | The op is allowed on the store (`readonly`) |
| V032 | A literal key or `to_key` is safe |
| V033 | `presign.expires_in` is within the store's `presign_max_seconds` |
| V034 | The `Storage` file is valid: provider fields, `${ENV}` secrets, `https` endpoint |
| V037 | `http.upload` is consistent: a `file` input exists, the store exists, is writable and holds `max_bytes` |

An op that is missing a field, or has one it does not take, is a schema error (V001). V004 requires
routing `not_found` and `conflict` where they can occur, and V007 checks the output type
(`json` for a blob, `str` for `get` text or base64 and `presign`, `json` for `get ... as: json`,
`list[json]` for `list`).

## Command line and Insight

```bash
tuvl check storage            # list, put, head, get, delete a probe object in every store
tuvl check storage invoices
tuvl list objects invoices 2026/
tuvl check system                   # also lists stores and the environment variables they still need
```

**Insight → Configure → Storage** lists the stores, shows missing environment variables, checks a
connection and browses objects read-only. New stores are YAML files in `storage/`.

## Operations

- **Retries.** Connection errors, timeouts, 429 and 5xx are retried up to three times with backoff;
  these operations are idempotent, and exclusive writes use a precondition
  (`If-None-Match: *`, `ifGenerationMatch=0`) so a retry cannot overwrite.
- **Telemetry.** Each call is an OpenTelemetry span `storage <op>` with the provider, store and op.
  Keys and URLs are never attributes.
- **Local stores in production.** `tuvl ship` adds one PersistentVolumeClaim (`storage.local` in
  `values.yaml`: `size`, `storageClass`, `accessMode`, `existingClaim`) and mounts it at each local
  store's root, with a pod `fsGroup` so the non-root user can write. With `split: true` the API and
  worker pods share it: set `storage.local.accessMode=ReadWriteMany` and a class that supports it.
  Cloud stores need no volume. `storage/` is on the image allowlist; a project shipped earlier needs
  `tuvl ship --force` once (ship warns when `storage/` would be left out).
- **Limits.** Tool and `ctx.storage` calls move whole objects (default 16 MiB, at most 64 MiB). Server-side
  `copy` is native on S3-compatible stores and GCS; on Azure it reads and writes through the engine.

## Testing against emulators

The opt-in tests in `tests/storage/test_services.py` run one contract suite against real services and
skip any that are not running:

```bash
docker run -d -p 19000:9000 -e RUSTFS_ACCESS_KEY=testkey -e RUSTFS_SECRET_KEY=testsecret123 rustfs/rustfs /data
docker run -d -p 10000:10000 mcr.microsoft.com/azure-storage/azurite azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck
docker run -d -p 14443:4443 fsouza/fake-gcs-server -scheme http -port 4443 -public-host localhost:14443
uv run pytest tests/storage
```
