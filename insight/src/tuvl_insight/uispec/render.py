"""The UI specs as Markdown: one index, one file per ready workflow, one for the CRUD models."""

from __future__ import annotations

import json
from typing import Any

from tuvl_insight.uispec.facts import digest

NOTES = "<!-- tuvl:notes — everything below this line is yours and is kept when this file is regenerated -->"
DEFAULT_BASE = "http://localhost:8885"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def _json(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n```\n"


def _front(kind: str, name: str, facts: Any) -> str:
    return f"---\nui_spec: {kind}\nname: {name}\ncontract_hash: {digest(facts)}\ngenerated_by: tuvl generate ui-spec\n---\n"


def _auth(w: dict[str, Any]) -> str:
    a = w["auth"]
    if a["public"]:
        return "none (public)"
    needs = [
        f"scope `{a['scope']}`" if a["scope"] else "",
        f"group `{a['group']}`" if a["group"] else "",
    ]
    needs = [n for n in needs if n]
    return "a bearer token" + (" with " + " and ".join(needs) if needs else "")


def _state(status: int) -> str:
    if status < 300:
        return "Success: show the result."
    if status < 500:
        return "A refusal the person can act on: show the reason, let them change the input."
    return "A failure: say it did not complete and offer to retry."


def _workflow_md(w: dict[str, Any], base: str, specs: list[str]) -> str:
    name, http = w["name"], w["http"]
    out = [_front("workflow", name, w)]
    out.append(f"# UI spec — `{name}`\n")
    if w["description"]:
        out.append(f"{w['description']}\n")
    if specs:
        out.append(
            f"Derived from {', '.join(f'`{s}`' for s in specs)}. Written by `tuvl generate ui-spec` from the implemented workflow; "
            "regenerate it after the backend changes.\n"
        )
    out.append("## Endpoint\n")
    out.append(
        _table(
            ["", ""],
            [
                [
                    "Call",
                    f'`{http["method"]} {http["path"]}` (or `POST /api/workflows/{name}/runs` with `{{ "input": … }}`)',
                ],
                [
                    "Which",
                    "The first route suits a plain frontend, or a gateway that allows exact paths. "
                    "`@tuvl/client` starts every workflow through the second (`execute`, or `start` to get a handle at once), "
                    "so it needs no per-workflow routing",
                ],
                [
                    "Auth",
                    _auth(w) + ("; `iam:admin` also passes" if not w["auth"]["public"] else ""),
                ],
                ["Effect", f"`{w['effect']}`"],
                [
                    "Answer",
                    f"The end's body when the run finishes within {http['sync_timeout']}; otherwise `202` with a run descriptor to follow",
                ],
            ],
        )
    )
    out.append("## Request\n")
    rows = [
        [
            f"`{f['name']}`",
            f"`{f['type']}`",
            "yes" if f["required"] else "no",
            "in the path"
            if f["name"] in w["input"]["in_path"]
            else (
                "file part (multipart)"
                if f["type"] in ("file", "list[file]")
                else ("query" if http["method"] in ("GET", "DELETE") else "body")
            ),
        ]
        for f in w["input"]["fields"]
    ]
    out.append(_table(["Field", "Type", "Required", "Where"], rows) or "No input.\n")
    if w.get("upload"):
        out.append(_upload_md(w))
    ex = w["examples"]
    if ex:
        out.append(
            "Real examples from the spec's acceptance cases (use them as fixtures and mock data):\n"
        )
        for e in ex:
            out.append(f"**{e['name']}**" + (f" → ends at `{e['end']}`" if e["end"] else "") + "\n")
            out.append(_json(e["input"]))
    else:
        out.append("An illustration:\n")
        out.append(_json(w["input"]["example"]))
    out.append("## What can come back\n")
    rows = []
    for r in w["responses"]:
        fields = ", ".join(f"`{f['name']}`" for f in r["fields"]) or "—"
        rows.append(
            [
                f"`{r['status']}`",
                ", ".join(f"`{e}`" for e in r["ends"]),
                fields,
                _state(r["status"]),
            ]
        )
    rows += [
        [
            "`202`",
            "run not finished",
            "`run_id`, `status`, `status_url`, `events_url`, `approval?`",
            "Show progress; follow the run (events) or poll `status_url` until it ends.",
        ],
        ["`401`", "—", "—", "Not signed in or the token expired: sign in again."],
        [
            "`403`",
            "—",
            "—",
            "Signed in but not allowed (missing scope or group): show a no-access state, do not retry.",
        ],
        [
            "`409`",
            "`workflow_unavailable`",
            "`reasons`",
            "Temporarily unavailable (a model it needs is disabled): show a try-later state.",
        ],
        [
            "`422`",
            "—",
            "`detail[]` per field",
            "Invalid input: show each message next to its field.",
        ],
    ]
    out.append(_table(["HTTP", "End", "Body fields", "What the UI does"], rows))
    for r in w["responses"]:
        if r["fields"]:
            out.append(f"Body of `{r['status']}` (illustration):\n")
            out.append(_json(r["example"]))
    if w["human"] or w["tool_approvals"]:
        out.append("## Waiting for a person\n")
        out.append(
            "This workflow can stop and wait, so a run's status may be `waiting_human` or `waiting_approval`. "
            "The `202` body then carries `approval` (title, form, `decide_url`). Whoever decides needs the "
            "`approvals:decide` scope plus the group below, and is never the person who started the run when a group is required.\n"
        )
        for h in w["human"]:
            out.append(f"### Step `{h['agent']}` — {h['title']}\n")
            if h["instruction"]:
                out.append(f"{h['instruction']}\n")
            facts = [
                f"decided by group `{h['group']}`"
                if h["group"]
                else "decided by anyone with `approvals:decide`",
                f"expires after {h['expires']}" if h["expires"] else "",
            ]
            out.append("- " + "\n- ".join(f for f in facts if f) + "\n")
            out.append(
                _table(
                    ["Form field", "Widget", "Label"],
                    [[f"`{f['name']}`", f["widget"], f["label"]] for f in h["form"]],
                )
            )
        if w["tool_approvals"]:
            out.append(
                f"Tool calls in {', '.join(f'`{a}`' for a in w['tool_approvals'])} wait for `approve`, `edit` or `reject`.\n"
            )
        out.append(
            "```ts\nconst open = await client.approvals.list();               // what this person may decide\n"
            "await client.approvals.decide(open[0].approval_id, { values: { /* the form */ } });\n```\n"
        )
    out.append("## Build it with `@tuvl/client`\n")
    out.append(_client_snippet(w, base))
    out.append("## Checklist\n")
    out.append(
        "- [ ] Every status in the table above has a visible state (success, refusal, failure, waiting, no access, sign in).\n"
        "- [ ] A `202` is followed to its end (events or polling); the screen survives a refresh using the `run_id`.\n"
        "- [ ] Field errors (`422`) appear next to their inputs.\n"
        "- [ ] The token lives in memory or a secure cookie, never in the repository or the bundle.\n"
    )
    out.append("\n" + NOTES + "\n")
    return "\n".join(out)


def _upload_md(w: dict[str, Any]) -> str:
    up = w["upload"]
    accept = ", ".join(f"`{a}`" for a in up["accept"]) or "any type"
    names = ", ".join(f"`{n}`" for n in up["files"])
    return (
        "### Uploading files\n\n"
        f"Send the request as `multipart/form-data`: one file part per file input ({names}), and the other "
        "fields as text parts (a list or object field is sent as JSON text). A JSON body cannot carry a file.\n\n"
        f"- At most {up['max_files']} files per request, {up['max_bytes']} bytes each; accepted types: {accept}.\n"
        "- Too large is `413`, a refused content type `415`, a field that is not a file input `422`.\n"
        "- The server keeps the file and gives the workflow a reference (`name`, `size`, `content_type`, "
        "`sha256`); the file's own address is not returned, so show `name` and `size` to the person.\n\n"
        "```ts\n"
        "// @tuvl/client builds the multipart body when a value is a File or Blob\n"
        f'await client.execute("{w["name"]}", {{ {up["files"][0]}: fileInput.files[0], /* …other fields */ }});\n'
        "```\n"
    )


def _client_snippet(w: dict[str, Any], base: str) -> str:
    inp = json.dumps(
        w["examples"][0]["input"] if w["examples"] else w["input"]["example"], indent=2
    )
    inp = inp.replace("\n", "\n  ")
    return (
        "```ts\n"
        'import { RunHandle, TuvlApiError, TuvlClient, TuvlRunError } from "@tuvl/client";\n'
        'import type { Workflows } from "./tuvl.generated";\n\n'
        f'const client = new TuvlClient<Workflows>({{ baseUrl: "{base}", token }});\n\n'
        "try {\n"
        f'  const result = await client.execute("{w["name"]}", {inp});\n'
        "  if (result instanceof RunHandle) {\n"
        "    for await (const event of result.events()) showProgress(event); // agent_started, decision, …\n"
        "    showResult(await result.wait());\n"
        "  } else {\n"
        "    showResult(result); // the end's body\n"
        "  }\n"
        "} catch (error) {\n"
        "  if (error instanceof TuvlRunError) showFailure(error);        // the run ended in an error end\n"
        "  else if (error instanceof TuvlApiError) showRefusal(error);   // 401, 403, 409, 422 …\n"
        "  else throw error;\n"
        "}\n"
        "```\n"
    )


def _models_md(models: list[dict[str, Any]]) -> str:
    out = [
        _front("models", "models", models),
        "# UI spec — models with CRUD routes\n",
        'These models opt in to REST routes under `/models/<name>/`. Use `client.crud("<name>")` from `@tuvl/client`; '
        "the name is lower-cased in the path.\n",
    ]
    for m in models:
        low = m["name"].lower()
        out.append(f"## {m['name']}\n")
        ops = {
            "create": ("POST", f"/models/{low}/", m["scopes"]["write"], m["groups"]["write"]),
            "list": ("GET", f"/models/{low}/", m["scopes"]["read"], m["groups"]["read"]),
            "read": ("GET", f"/models/{low}/{{id}}", m["scopes"]["read"], m["groups"]["read"]),
            "update": (
                "PATCH",
                f"/models/{low}/{{id}}",
                m["scopes"]["write"],
                m["groups"]["write"],
            ),
            "delete": (
                "DELETE",
                f"/models/{low}/{{id}}",
                m["scopes"]["delete"],
                m["groups"]["delete"],
            ),
        }
        rows = [
            [op, f"`{v[0]} {v[1]}`", f"`{v[2]}`", ", ".join(f"`{g}`" for g in v[3]) or "—"]
            for op, v in ops.items()
            if op in m["operations"]
        ]
        out.append(_table(["Operation", "Call", "Scope", "Group (also needed)"], rows))
        out.append(
            "List takes `limit` (1–1000, default 100), `offset`, `filter[field]=value` and `include=relation,…`. `iam:admin` passes every check.\n"
        )
        out.append(
            _table(
                ["Field", "Type", "Required", "Sent on create", "Notes"],
                [
                    [
                        f"`{f['name']}`",
                        f"`{f['type']}`",
                        "yes" if f["required"] else "no",
                        "yes" if f["in_create"] else "no (server-set)",
                        "secure: do not log, echo or cache"
                        if f["secure"]
                        else ("primary key" if f["primary_key"] else ""),
                    ]
                    for f in m["fields"]
                ],
            )
        )
        out.append(
            f'```ts\nconst {low} = client.crud("{low}");\n'
            f"const rows = await {low}.list({{ limit: 20, filters: {{ /* field: value */ }} }});\n```\n"
        )
    out.append("\n" + NOTES + "\n")
    return "\n".join(out)


_GROUP_USE = {
    "auth": "`/auth/*`: sign in, refresh, who am I",
    "runs": "`/api/runs/*`, `/api/approvals/*` and `POST /api/workflows/{name}/runs`: start, follow and decide runs",
    "admin": "`/admin/*`: engine administration",
    "artifacts": "`/api/artifacts/*`: artifact list and upload",
    "system": "`/api/_system/*`: introspection, meant for Insight",
}


def _surface_md(surface: dict[str, Any]) -> str:
    groups = surface.get("groups") or ["auth", "runs"]
    docs = surface.get("docs", "off")
    lines = [
        "A production server of this project mounts these system routes, and nothing else besides the "
        "workflow routes and model CRUD listed in these files. Build a UI on them only:\n"
    ]
    lines += [f"- {_GROUP_USE[g]}" for g in groups if g in _GROUP_USE]
    lines.append(
        "\nDo not call `/api/_system`, `/admin` or `/api/artifacts` from a UI unless they are listed above; "
        "a production server answers `404` for the rest.\n"
    )
    lines.append(
        {
            "off": "Production serves no OpenAPI document (`/docs`, `/openapi.json`), so generate the typed client "
            "from `tuvl dev` or a staging server that has `spec.api.docs: workflows`, not from production.\n",
            "workflows": "Production serves an OpenAPI document with the workflow routes only, so the typed client can "
            "be generated from any environment of this backend.\n",
            "all": "Production serves an OpenAPI document that includes the system routes; generate the typed client "
            "from any environment of this backend.\n",
        }.get(docs, "")
    )
    cors = surface.get("cors") or []
    if "*" in cors:
        lines.append(
            "\nBrowser apps on any origin may call this backend (`spec.api.cors_origins` contains `*`).\n"
        )
    elif cors:
        lines.append(
            "\nBrowser apps may call this backend from "
            + ", ".join(f"`{o}`" for o in cors)
            + ". Any other origin is blocked by the browser.\n"
        )
    else:
        lines.append(
            "\nThis backend sends no CORS headers, so a browser app on another origin is blocked. Serve the UI from the "
            "backend's origin (a reverse proxy for `/api`, `/auth` and `/models`; a dev-server proxy in development), or ask "
            "for the UI's origin to be added to `spec.api.cors_origins`.\n"
        )
    return "\n".join(lines)


def _index_md(facts: dict[str, Any], base: str, shown: list[str]) -> str:
    out = [
        _front(
            "index",
            facts["project"],
            {
                "workflows": [(w["name"], w["ready"]) for w in facts["workflows"]],
                "models": [m["name"] for m in facts["models"]],
            },
        ),
        f"# UI specs — {facts['project']}\n",
        "How to build the user interface of this backend. Everything here is written from the implemented project by "
        "`tuvl generate ui-spec`: a file appears for each workflow once its tasks are done, so what is documented is what exists. "
        "Read this file first, then the file of the workflow or model you are building a screen for.\n",
        "## Build with `@tuvl/client`\n",
        "Use the official client: it knows the run lifecycle (a `202`, events, waiting for a person), approvals, CRUD and "
        "sign-in, so a UI does not re-implement them.\n",
        "```bash\nnpm install @tuvl/client\n"
        f"npx tuvl-client generate --url {base} --out src/tuvl.generated.ts   # typed workflows from this backend's OpenAPI\n```\n",
        '```ts\nimport { TuvlAuth, TuvlClient } from "@tuvl/client";\nimport type { Workflows } from "./tuvl.generated";\n\n'
        f'const auth = new TuvlAuth({{ baseUrl: "{base}" }});   // POST /auth/token, refresh, logout\n'
        f'const client = new TuvlClient<Workflows>({{ baseUrl: "{base}", token }});\n```\n',
        "## Rules for whoever builds the UI (developer or coding agent)\n",
        "1. Call only the endpoints in these files; do not guess routes or fields. If something is missing, the backend does not have it yet.\n"
        "2. Use `@tuvl/client` for runs, events, approvals, CRUD and sign-in rather than hand-written `fetch`.\n"
        "3. Handle every status each workflow can return, including `202`, `401`, `403`, `409` and `422`.\n"
        "4. Never put a signing key, a token or a `secure` field into the repository, the bundle, logs or analytics.\n"
        "5. Authorization is the backend's job; the UI only decides what to show. Hide actions the token's scopes do not allow, "
        "but expect a `403` anyway.\n"
        "6. Use the real examples in each file as fixtures for UI tests; they are the backend's own acceptance cases.\n"
        "7. Call only the routes under Backend surface and in the workflow and model files. Never call `/api/_system`, `/admin` or `/api/artifacts` unless the surface lists them.\n",
        "## Workflows\n",
    ]
    rows = []
    for w in facts["workflows"]:
        link = f"[`{w['name']}`]({w['name']}.md)" if w["name"] in shown else f"`{w['name']}`"
        what = (
            "ready"
            if w["ready"]
            else "not ready: "
            + ", ".join(
                [f"pending step `{a}`" for a in w["missing"]["pending_agents"]]
                + [f"task `{t}`" for t in w["missing"]["open_tasks"]]
            )
        )
        rows.append([link, f"`{w['http']['method']} {w['http']['path']}`", _auth(w), what])
    out.append(
        _table(["Workflow", "Call", "Auth", "State"], rows)
        or "No workflow has an HTTP route yet.\n"
    )
    if facts["not_callable"]:
        out.append(
            "Not started by a UI (scheduled or internal): "
            + ", ".join(f"`{n}`" for n in facts["not_callable"])
            + ".\n"
        )
    out.append("## Models\n")
    if facts["models"] and "models" in shown:
        out.append(
            "[Models with CRUD routes](models.md): "
            + ", ".join(f"`{m['name']}`" for m in facts["models"])
            + ".\n"
        )
    else:
        out.append("No model exposes CRUD routes.\n")
    out.append("## Backend surface\n")
    out.append(_surface_md(facts.get("surface") or {}))
    out.append("## Shared behaviour\n")
    out.append(
        _table(
            ["Topic", "What to know"],
            [
                ["Base URL", f"`{base}` in development; configure it per environment"],
                [
                    "Sign in",
                    "`POST /auth/token` (OAuth2 password form, `username` is the email) → a JWT; send it as `Authorization: Bearer …`; `GET /auth/me` shows its scopes and groups",
                ],
                [
                    "Following a run",
                    "`GET /api/runs/{id}` (status) and `GET /api/runs/{id}/events` (Server-Sent Events, resumable with `Last-Event-ID`)",
                ],
                [
                    "Run statuses",
                    "`running`, `waiting_human`, `waiting_approval`, `paused`, then a terminal one: `completed`, `failed`, `aborted`, `compensated`",
                ],
                [
                    "Errors",
                    "`401` not signed in, `403` not allowed, `409 workflow_unavailable`, `422` invalid input with `detail[]`",
                ],
            ],
        )
    )
    out.append("\n" + NOTES + "\n")
    return "\n".join(out)


def render(
    facts: dict[str, Any], *, base_url: str = DEFAULT_BASE, include_partial: bool = False
) -> dict[str, str]:
    """``{relative path: text}`` for every file this project's UI specs consist of."""
    files: dict[str, str] = {}
    shown: list[str] = []
    for w in facts["workflows"]:
        if w["ready"] or include_partial:
            files[f"{w['name']}.md"] = _workflow_md(w, base_url, facts["specs"])
            shown.append(w["name"])
    if facts["models"]:
        files["models.md"] = _models_md(facts["models"])
        shown.append("models")
    files["README.md"] = _index_md(facts, base_url, shown)
    ready = {
        "project": facts["project"],
        "workflows": [w for w in facts["workflows"] if w["name"] in shown],
        "models": facts["models"],
    }
    files["ui-contract.json"] = json.dumps(ready, indent=2, ensure_ascii=False, default=str) + "\n"
    return files
