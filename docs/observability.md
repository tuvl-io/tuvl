# Observability — Structured Logging & OpenTelemetry

tuvl ships enterprise-grade observability out of the box: structured JSON logging via **structlog** and distributed tracing via **OpenTelemetry** (OTel). Both are active in production and automatically disabled in dev mode.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Structured Logging](#2-structured-logging)
3. [Distributed Tracing](#3-distributed-tracing)
4. [Span Hierarchy](#4-span-hierarchy)
5. [Model-call spans](#5-model-call-spans)
6. [Metrics](#6-metrics)
7. [Configuration Reference](#7-configuration-reference)
8. [Collector Setup](#8-collector-setup)
9. [Dev Mode Behaviour](#9-dev-mode-behaviour)
10. [Data Masking & PII](#10-data-masking--pii)

---

## 1. Architecture Overview

```
trigger (HTTP / API / MCP / schedule / child run)
    │  FastAPI HTTP span (W3C traceparent propagated)
    ▼
tuvl.run                      one span per execution of a run (each claim: start, resume)
├── tuvl.agent <id>           one per agent invocation
│   └── chat <model>          gen_ai.* spans for model calls (llm, loop turns, decide models, judges)
└── tuvl.agent <id> …
```

Two complementary records:

- **The journal** (`tuvl_system_run_events`) is the authoritative, durable record of what a run did —
  every agent start/finish, signal, model turn, tool call, decision, judge verdict, approval and
  pause. It drives the Insight Runs page, `GET /api/runs/{id}/events` (SSE), `tuvl follow run`,
  replay and test fixtures. See [runtime](runtime.md#journal-events).
- **OpenTelemetry spans and structured logs** feed your tracing and log backends, correlated by
  `trace_id`/`span_id` and `run_id`.

---

## 2. Structured Logging

tuvl logs with structlog: JSON lines in production, a readable console format in development.

### Log format — production

```json
{
  "event": "Workflow execution started",
  "level": "info",
  "logger": "tuvl.core.engine.runner",
  "timestamp": "2026-05-26T10:00:00.000000Z",
  "workflow": "recruitment-pipeline",
  "trace_id": "4a8f3e...",
  "span_id": "8b2c1d..."
}
```

### Log format — development (`TUVL_ENV=development`)

```
2026-05-26T10:00:00Z [info     ] Workflow execution started   workflow=recruitment-pipeline trace_id=4a8f3e...
```

### Key log events

| Event | Level | Fields |
|---|---|---|
| `runtime.started` | info | `role`, `agents`, `triggers`, `mcp_tools`, `workflows` |
| `runtime.worker.started` | info | worker id, concurrency |
| `runtime.trigger.mounted` | info | `workflow`, `method`, `path` |
| `runtime.mcp.mounted` | info | `tools` |
| `runtime.lease.lost` | warning | the run moved to another worker |
| `runtime.lease.cancelled` | warning | the running step was cancelled because the run is no longer this worker's |
| `runtime.lease.expired` | error | a whole lease passed without a good heartbeat (the database was unreachable) |
| `runtime.heartbeat.failed` | warning | a heartbeat raised; it is retried |
| `runtime.worker.poll_failed` | error | a claim raised; the poll loop goes on after a backoff |
| `runtime.sweep.failed` | error | a housekeeping sweep raised; it is skipped |
| `runtime.worker.ended` | error | the poll loop ended unexpectedly; `/health` answers 503 |
| `scheduler.schedule_dropped` | error | a schedule that cannot advance is no longer fired; the others go on |
| `runtime.runs.will_fail` | warning | at boot: `workflow`, `runs`, `error_type`: unfinished runs of a workflow that changed (or is gone) since they started |
| `runtime.pause_ignored` | warning | pause requested on a `per_run` transaction |
| `decide.shadow_model_failed` | warning | `agent`, `error` (shadow failures never fail the run) |
| `decision.provider.load_failed` | warning | a decision provider entry point failed to load |
| `judge.cache_write_failed` | warning | read-only checkout; verdicts still computed |
| `mcp.schema_drift` | warning | a live MCP tool schema differs from `tuvl.lock` (once per tool) |

Per-run detail (what each agent did) is in the journal, not the logs.

### Logging from code agents

```python
from tuvl import agent, Ctx


@agent("enrich")
async def enrich(inp, ctx: Ctx):
    ctx.log.info("enrich.lookup", candidate_id=str(inp.candidate.id))  # bound to run_id and agent
    ...
```

Pass data as keyword arguments, never f-strings, so values are structured fields.

---

## 3. Distributed Tracing

tuvl uses the OpenTelemetry SDK with an OTLP exporter; `init_telemetry()` installs the
`TracerProvider` at startup. Inbound W3C `traceparent` headers make run spans children of the caller's
trace.

### Span attributes set by tuvl

| Attribute | Span | Description |
|---|---|---|
| `tuvl.run_id` | `tuvl.run` | The run |
| `tuvl.workflow` | `tuvl.run`, `tuvl.agent` | Workflow name |
| `tuvl.agent` | `tuvl.agent` | Agent id |
| `tuvl.engine` | `tuvl.agent` | `code`, `tool`, `decide`, `llm`, `loop`, `human`, `map`, `parallel`, `pending` |
| `tuvl.determinism` | `tuvl.agent` | `deterministic`, `bounded`, `external`, `pending` |
| `tuvl.signal` | `tuvl.agent` | The emitted signal |
| `tuvl.tokens` | `tuvl.agent` | Tokens used by the invocation |
| `tuvl.attempt` | `tuvl.agent` | Retry attempt |
| `tuvl.error_type` | `tuvl.agent` | On `error` (span status ERROR) |

Context values are never span attributes, so secure fields can't leak into traces.

---

## 4. Span Hierarchy

A run that pauses (a human form, a tool approval, a breakpoint) and resumes on another worker produces
one `tuvl.run` span per execution segment, all carrying the same `tuvl.run_id`; group by it to see the
whole run. Engine work — model calls, HTTP calls, DB queries — nests under the agent's span.

---

## 5. Model-call spans

The built-in model client wraps every call in a span named `chat <model>` (`embeddings <model>` for
embeddings) using the [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) —
`gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons` — nested under the `tuvl.agent` span
that made them. A failed call records the exception on the span.

---

## 6. Metrics

Run-level numbers (runs by status and end, tokens per agent, decision sources, approvals) come from
the journal and the runs table: `GET /api/runs`, `tuvl list runs --json`, or SQL over
`tuvl_system_runs` / `tuvl_system_run_events`. The one OTel counter is `tuvl.hook.events`
(meter `tuvl.hooks`), incremented when a `type: hook` artifact with `action: metric` fires
(`tuvl.hook.name`, `tuvl.hook.event`, `tuvl.workflow.name`).

---

## 7. Configuration Reference

All settings are read from `.env` or environment variables. Settings also support a per-project YAML override file at `.tuvl/telemetry.yaml` (see below).

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `TUVL_TELEMETRY_ENABLED` | `true` | Set `false` to disable all OTel export |
| `TUVL_OTLP_ENDPOINT` | `http://localhost:4318` | OTLP collector endpoint (base URL; tuvl adds `/v1/traces` and `/v1/metrics`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Standard OTel env var — takes precedence over `TUVL_OTLP_ENDPOINT` when set |
| `TUVL_OTLP_PROTOCOL` | `http/protobuf` | `http/protobuf` or `grpc` (needs `pip install 'tuvl[otel-grpc]'`) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | — | Standard OTel env var — takes precedence over `TUVL_OTLP_PROTOCOL` when set |
| `TUVL_SERVICE_NAME` | `tuvl` | `service.name` resource attribute in every span |
| `TUVL_ENV` | `production` | `production` → JSON logs; `development` → coloured console logs. Case and padding do not matter; a value other than `production`, `development`, `test` or `staging` is logged at boot and treated as not production |

### `.tuvl/telemetry.yaml` override

Values from this file are applied only when the corresponding environment variable is not set, making it suitable for checked-in defaults that operators can override at deployment time.

```yaml
kind: TelemetryConfig
version: v1
spec:
  enabled: true
  otlp_endpoint: http://otel-collector:4318
  otlp_protocol: http/protobuf
  service_name: my-tuvl-app
```

### Config resolution order

For each setting, the first non-empty source wins:

1. Environment variable (`TUVL_TELEMETRY_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `TUVL_OTLP_ENDPOINT`, `TUVL_SERVICE_NAME`)
2. `.tuvl/telemetry.yaml` spec fields
3. Hardcoded defaults (enabled=`true`, endpoint=`http://localhost:4318`, protocol=`http/protobuf`, service=`tuvl`)

---

## 8. Collector Setup

tuvl exports spans and metrics over **OTLP/HTTP** (port 4318) to any OTLP-compatible collector. To use gRPC (port 4317) instead, install `tuvl[otel-grpc]` and set `TUVL_OTLP_PROTOCOL=grpc`. If you upgraded from a version that used gRPC and your endpoint still ends in `:4317`, tuvl logs a warning at startup. Below are quick-start configurations for common backends.

### OpenTelemetry Collector (recommended)

```yaml
# docker-compose.yml
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    ports:
      - "4318:4318"   # OTLP/HTTP
    volumes:
      - ./otel-config.yaml:/etc/otelcol/config.yaml

  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "16686:16686"  # Jaeger UI
```

```yaml
# otel-config.yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

exporters:
  jaeger:
    endpoint: jaeger:14250
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [jaeger]
```

```bash
# .env
TUVL_OTLP_ENDPOINT=http://localhost:4318
TUVL_SERVICE_NAME=my-app
```

### Grafana Tempo (via Alloy)

```bash
TUVL_OTLP_ENDPOINT=http://alloy:4318
TUVL_SERVICE_NAME=my-app
```

### Honeycomb

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
# Set OTEL_EXPORTER_OTLP_HEADERS with your API key via collector or proxy
```

### Datadog

Use the Datadog Agent as an OTLP collector:

```bash
TUVL_OTLP_ENDPOINT=http://datadog-agent:4318
```

---

## 9. Dev Mode Behaviour

When `TUVL_DEV_MODE=true` (set automatically by `tuvl dev`):

- `init_telemetry()` is a **no-op** — no `TracerProvider` is installed
- The OTel SDK default `NonRecordingSpan` is returned for all `start_as_current_span` calls — all span operations become no-ops with negligible overhead
- Logs use the **coloured ConsoleRenderer** instead of JSON
- `trace_id` / `span_id` are **not** injected into log events (no valid span context)

Dev mode never runs with `TUVL_ENV=production` — that combination aborts at boot.

---

## 10. Data Masking & PII

Fields declared `secure: true` in a `ModelDefinition`:

```yaml
# models/employee.yaml
spec:
  fields:
    - { name: national_id, type: string, secure: true }
```

- are **redacted** (`"*****"`) in journal events, SSE streams, run exports, pinned fixtures and the
  Insight views; checkpoints and a run's input keep real values because they are the resume state;
- are never span attributes (tuvl puts no context values on spans);
- are **left out of CRUD responses** unless the caller holds `<model>:read_secure` or `iam:admin`, in the
  record and in related records an `?include=` embeds (`docs/auth.md`);
- are **not sent to models** (`llm`, `loop`) unless `policy.allow_secure_to_llm: true`: a secure field in a
  model agent's inputs is a V020 error, and at run time the value is replaced by `*****` in the prompt (its
  template and its input block) and in every tool result a loop agent reads. This masks by field *name*, because
  the data a prompt or a tool result carries has no model attached: a key that shares a secure field's name is
  masked wherever it appears. The CRUD API uses the per-model registry, so there a field is hidden only in the model
  that declares it `secure`.

Two registries are built at startup from every loaded `ModelDefinition`: `SECURE_FIELDS` (the names) and
`SECURE_BY_MODEL` (model → names).
