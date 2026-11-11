# Project identity and the catalogue

Every project made by `tuvl init` carries `.tuvl/project.yaml`: who built it, what it is, and a
stable id. A project can optionally be listed in the public catalogue (nest.tuvl.io). Nothing is sent
anywhere unless you register.

## The file

```yaml
kind: Project
version: v1
metadata:
  name: screening
spec:
  id: prj_01a141a1-9446-74f6-81e4-bc37135cfcf1-f8
  author: Jane Doe
  organization: Acme
  organization_url: https://acme.example   # optional
  email: jane@example.com
  type: dev                # poc | dev | production
  version: 0.1.0           # the app's own semver, not the engine's
  description: |
    Screens **candidate** applications.

    - scores CVs
    - routes edge cases to a human
  license: Apache-2.0             # optional
  repo: https://github.com/acme/screening
  registry:                # written by register, not by hand
    url: https://nest.tuvl.io
    id: prj_01a141a1-9446-74f6-81e4-bc37135cfcf1-f8
    registered_at: 2026-10-15T10:00:00Z
    digest: 3f9a1c0b7d2e4a55
```

| Field | Rules |
|---|---|
| `id` | Minted once by `tuvl init`; never changes. `prj_` + a UUIDv7 + two check characters |
| `name` | Required |
| `type` | `poc`, `dev` or `production` |
| `version` | Semver (`1.2.3`, `1.2.3-rc.1`) |
| `author`, `email` | Captured and sent to the Nest on registration so the owner can be reached; the Nest keeps them private and never shows them. The email must be a valid address if set |
| `organization`, `organization_url` | The organization the project belongs to, and an optional `http(s)` link to it. Both are shown in the catalogue |
| `description` | **Markdown**, up to 2000 characters |
| `repo` | `http(s)` URL if set |

The file is read fail-open: a project without it boots normally and `tuvl upgrade project` creates it.

### The id is self-checking

The two trailing characters are derived from the UUID. A typo or a half-edited id fails the check
and every project command (`show`, `set`, `register`), Insight and register refuse it. This guards against accidents and
copy-paste damage; it is **not a signature**: anyone can recompute the check. Ownership of a
listing is decided by the Nest: whoever holds the listing's entry secret may update it, and a claim on a listed id counts only if the Nest itself sees the verification tag.

A copied project shares its id with the original. Run `tuvl upgrade project --new-id` in the copy to
mint its own (this also clears the registration).

## Command line

```bash
tuvl show project [--json]            # the record; description rendered as Markdown
tuvl set project author "Jane Doe"
tuvl set project description @about.md          # read from a file
cat README.md | tuvl set project description -  # or stdin
tuvl upgrade project [--name N] [--new-id]
tuvl register project [--nest URL] [--remote NAME] [--no-verify] [--keep-tag] [-y]
```

`show project` also reports the registry state (not registered, up to date, changed since the last
registration, or id differs from the registered one) and the listing's label.

Listing is **optional and developer-only**: `register project` exists only where `tuvl-insight` is
installed, never in a production install. There is no account and no sign-in; see
[Listing and labels](#listing-and-labels).

## Insight

**Settings → Project** edits every field above. The description has an Edit / Preview toggle.

The **Catalogue** card shows the listing state, its label and a **Register** button (**Update listing**
once listed, and the card says when the project changed since). The dialog shows the exact payload and, when
verification can run, a checkbox: push a one-time tag to the project's git remote with your own git credentials,
then remove it. **Send** registers and, if that is ticked, verifies; the result names the label. The dialog says
why verification cannot run when it cannot (no repository URL, an `http` URL, no matching remote).

## What registration sends

```json
{
  "id": "01a141a1-9446-74f6-81e4-bc37135cfcf1",
  "name": "screening", "author": "Jane Doe", "organization": "Acme", "organization_url": "https://acme.example", "type": "dev", "version": "0.1.0",
  "description": "…", "license": "Apache-2.0", "repo": "https://…", "tuvl_version": "2.0.0"
}
```

plus `organization` and, when set, `contact_email`. The Nest lists the name, version, organization (with its link), type, licence, description and, once verified, the repository link; the author and email stay private. The Nest keys the entry by the
bare UUID; the check characters never leave your machine. The Nest is `https://nest.tuvl.io`, or `TUVL_NEST_URL` / `--nest` for a self-hosted one.

### Listing and labels

Registration is open: no account, no sign-in. The first registration returns an **entry secret**, shown once; it is what lets this
machine update the listing, and it is kept in `~/.config/tuvl/nest.json` (mode 0600), never in the project. In CI, set
`TUVL_NEST_ENTRY_SECRET`. Whoever holds the secret may update the listing; changing the repository URL resets its label.

The Nest also returns a one-time **challenge**. Unless `--no-verify` is given, `register project` shows the command, asks, then
creates the tag `tuvl-nest-verify-<hash>` on `HEAD`, pushes it to the remote (`--remote`, default `origin`, which must be the
project's `repo`) with your own git credentials, tells the Nest, and removes the tag again (`--keep-tag` leaves it). The tag
name is derived from a hash, so seeing it never reveals the challenge. The Nest then answers with a label:

| Label | Meaning |
|---|---|
| **Listed** | Self-declared. No push, or the push failed, or `--no-verify` |
| **Push confirmed** | This tool reported a successful push, but the Nest could not see it (a private repository), so it is not independently checked |
| **Verified** | The Nest itself ran `git ls-remote` on the public repository and found the tag |

Verified means the registrant controlled the repository when it was listed, not that the code is safe, and only a Verified listing
shows its repository link. Registering an id that is already listed without its entry secret is a **claim**: it changes nothing
unless the Nest sees its tag, and then it replaces an unverified owner; a Verified listing for another repository is never displaced.
A copied project shares its id (see above), so run `tuvl upgrade project --new-id` in the copy before listing it.

## Dev API (Insight)

Served by `tuvl dev` only; not mounted in production.

| Route | |
|---|---|
| `GET /dev/project` | the record plus `registration_state` (404 if the project has none) |
| `GET /dev/project/summary` | what the project holds: workflows, models, specs, tests, lock |
| `PUT /dev/project` | update editable fields; the id and registry block are ignored |
| `POST /dev/project/register/preview` | also returns what verification would do (`verification`: remote, URL, whether it matches `repo`, why not) |
| `POST /dev/project/register` | register; with `verify: true` (the developer's consent) also push the tag and report. Returns `label`, `claim`, `applied`, `pushed`, `notes` |
