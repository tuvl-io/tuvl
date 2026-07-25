# Observability — Structured Logging & OpenTelemetry

tuvl ships enterprise-grade observability out of the box: structured JSON logging via **structlog** and distributed tracing via **OpenTelemetry** (OTel). Both are active in production and automatically disabled in dev mode.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Structured Logging](#2-structured-logging)
3. [Distributed Tracing](#3-distributed-tracing)
4. [Span Hierarchy](#4-span-hierarchy)
5. [LiteLLM GenAI Telemetry](#5-litellm-genai-telemetry)
6. [Agent Metrics](#6-agent-metrics)
7. [Configuration Reference](#7-configuration-reference)
8. [Collector Setup](#8-collector-setup)
9. [Dev Mode Behaviour](#9-dev-mode-behaviour)
10. [Data Masking & PII](#10-data-masking--pii)

---

## 1. Architecture Overview

```
HTTP request
    │
    ▼
┌─────────────────────────────────────────────┐
│  FastAPI (automatic HTTP spans)             │  ← opentelemetry-instrumentation-fastapi
│  W3C traceparent header propagation         │  ← TraceContextTextMapPropagator
└───────────────────┬─────────────────────────┘
                    │
                    ▼
         workflow.execute span
                    │
         ┌──────────┴──────────┐
         ▼                     ▼
   node.Functional        node.Agent span
   span                        │
                               ▼
                     LiteLLM gen_ai.* spans    ← litellm OTel callback
                     (model, tokens, latency)
```

All log events emitted during a request are automatically annotated with `trace_id` and `span_id` so they can be correlated with spans in any OTel-compatible backend (Jaeger, Grafana Tempo, Honeycomb, Datadog, etc.).

---

## 2. Structured Logging

tuvl uses **structlog** for all log output. In production, every line is a single-line JSON object. In development mode a human-readable coloured renderer is used instead.

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

| Event | Level | Extra fields |
|---|---|---|
| `Workflow execution started` | info | `workflow` |
| `Executing workflow node` | info | `workflow`, `node_id`, `node_kind` |
| `Workflow execution complete` | info | `workflow`, `signal` |
| `Agent LLM call` | info | `step_id`, `model`, `attempt`, `max_attempts` |
| `Agent LLM response` | info | `step_id`, `model`, `input_tokens`, `output_tokens` |
| `Agent LLM retry` | info | `step_id`, `attempt`, `max_attempts`, `wait_s`, `last_signal` |
| `Agent LLM timeout` | warning | `step_id`, `timeout_s` |
| `Agent LLM error` | warning | `step_id`, `exc_type`, `error` |
| `Agent run complete` | info | `step_id`, `signal` |
| `agent.turn` | info | `step_id`, `iteration`, `max_iterations`, `tool_calls`, `tokens_used` |
| `agent.tool_call` | info | `step_id`, `iteration`, `tool`, `signal` |
| `agent.llm_error` | warning | `step_id`, `iteration`, `exc_type`, `error` |
| `agent.budget_exceeded` | warning | `step_id`, `tokens_used`, `token_budget` |
| `agent.max_iterations` | warning | `step_id`, `max_iterations` |
| `agent.guardrail_violation` | warning | `step_id`, `artifact`, `check`, `detail` |
| `workflow.hook` | info | `hook`, `event`, `workflow`, `step_id` (an `action: log` hook firing) |
| `OTel TracerProvider initialised` | info | `service`, `endpoint` |
| `FastAPI OpenTelemetry instrumentation active` | info | — |
| `LiteLLM OpenTelemetry callback registered` | info | — |

During an autonomous-mode `Agent` step, `run_streaming` also emits live **progress frames** over SSE/gRPC: `StepEvent`s with `signal="running"` and an `agent_progress` snapshot payload (`{type: iteration|tool_call|outcome, ...}`) — loop metadata only (no context values), so they add no PII surface beyond the masked final frame. The terminating frame carries the real outcome signal and the masked context snapshot as usual.

### Using the logger in custom nodes

```python
import structlog

logger = structlog.get_logger(__name__)

async def my_node(context):
    logger.info("Processing candidate", candidate_id=context["id"], step="enrich")
    # ...
    return context, "default"
```

Always pass extra data as keyword arguments — never use f-strings. This ensures the values appear as structured fields in JSON output and are automatically correlated with the active trace span.

---

## 3. Distributed Tracing

tuvl uses the **OpenTelemetry SDK** with a gRPC OTLP exporter. The global `TracerProvider` is installed at startup via `init_telemetry()` (called from the FastAPI lifespan).

### Propagation

Inbound HTTP requests are checked for a W3C `traceparent` header. When present, the new spans are created as children of the upstream trace, enabling end-to-end distributed traces across services.

```
# Example traceparent header
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
```

### Span attributes set by tuvl

| Attribute | Span | Description |
|---|---|---|
| `tuvl.workflow.name` | `workflow.execute` | Workflow YAML name |
| `tuvl.node.id` | `node.*` | Step ID from YAML |
| `tuvl.node.kind` | `node.*` | Step kind: `Agent`, `Functional`, `APICall`, etc. |
| `tuvl.step.signal` | `node.*` | Route signal returned by the step |
| `tuvl.step.duration_ms` | `node.*` | Wall-clock execution time in milliseconds |
| `tuvl.context.snapshot` | `node.*` | JSON snapshot of public context fields (PII masked) |
| `tuvl.agent.iteration` | `agent.iteration` | Loop iteration index (1-based) for an autonomous-mode `Agent` step |
| `tuvl.agent.tokens_used` | `agent.iteration` | Cumulative tokens consumed by the agent loop so far |
| `tuvl.agent.tool_calls` | `agent.iteration` | Number of tool calls the model requested this turn |
| `tuvl.agent.tool` | `agent.tool_call` | Name of the tool (component ref) invoked |
| `tuvl.agent.tool_signal` | `agent.tool_call` | Signal returned by the dispatched tool component |

---

## 4. Span Hierarchy

Every workflow execution produces a consistent span tree:

```
workflow.execute                               (1 span per workflow run)
├── node.Functional                            (1 per functional step)
├── node.Agent                                 (1 per Agent step — either mode)
│   │                                          mode: completion
│   ├── litellm.completion  [gen_ai.*]         (1+ per LLM call / retry)
│   │                                          mode: autonomous
│   ├── agent.iteration                        (1 per loop turn)
│   │   ├── litellm.completion  [gen_ai.*]     (the turn's LLM call)
│   │   └── agent.tool_call                    (1 per tool the model invoked this turn)
│   └── ...                                    (further iterations)
├── node.APICall
├── node.MCP
└── ...
```

The `node.{kind}` span is opened **before** the step executes and closed **after**, so that all LiteLLM calls made during an `Agent` step are automatically nested as children. This gives accurate per-step attribution of token usage and latency. The step span name is `node.<Kind>`, so autonomous agents appear as `node.Agent` — the mode is visible from the child spans.

For an autonomous-mode `Agent` step, each loop turn opens an `agent.iteration` span (carrying the iteration index and cumulative `tuvl.agent.tokens_used`), under which the turn's `litellm.completion` and any `agent.tool_call` spans nest — giving per-iteration cost and per-tool attribution across the whole agent loop. (These spans were named `autonomous_agent.*` before the agent unification.)

Every execution surface — engine run and streaming, Spectrum, and the test runner — funnels through one per-kind dispatch (`WorkflowEngine._run_kind`), so the span tree (and cross-cutting concerns like hooks) is identical regardless of how a workflow is executed; there are no per-surface dispatch forks to drift.

---

## 5. LiteLLM GenAI Telemetry

When telemetry is enabled, tuvl registers LiteLLM's built-in OpenTelemetry callback:

```python
litellm.callbacks = ["opentelemetry"]
```

LiteLLM (≥ 1.50) automatically picks up the globally registered `TracerProvider` and emits spans with [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

| Attribute | Description |
|---|---|
| `gen_ai.system` | Provider: `openai`, `anthropic`, etc. |
| `gen_ai.request.model` | Model name passed to the API |
| `gen_ai.usage.input_tokens` | Prompt token count |
| `gen_ai.usage.output_tokens` | Completion token count |
| `gen_ai.response.finish_reason` | Stop reason |

These spans appear as children of the `node.Agent` span (nested under `agent.iteration` in autonomous mode) in your tracing backend, giving you per-call token usage, latency, and cost attribution without any additional instrumentation code.

---

## 6. Agent Metrics

Alongside spans, the agent runtime emits **OTel counters** (meter `tuvl.agent`) so
agent health is dashboardable and alertable. They are created on a proxy meter at
import time and stay inert no-ops until `init_telemetry()` installs a
`MeterProvider` (production `run` mode); metrics export over the same gRPC OTLP
endpoint as traces via a `PeriodicExportingMetricReader`.

| Counter | Incremented when | Attributes |
|---|---|---|
| `tuvl.agent.iterations` | An autonomous agent loop completes a turn | `tuvl.agent.step_id` |
| `tuvl.agent.tool_calls` | The model invokes a declared tool | `tuvl.agent.step_id`, `tuvl.agent.tool` |
| `tuvl.agent.aborts` | A run is aborted by a supervisor or operator | `tuvl.agent.step_id` |
| `tuvl.agent.budget_exceeded` | A run hits its `token_budget` | `tuvl.agent.step_id` |
| `tuvl.agent.supervisor_actions` | A supervisor intervenes (abort / pause / steer) | `action`, `source` (rule kind or `judge`) |
| `tuvl.agent.judge_failures` | A supervisor LLM-judge call errors or times out | `workflow` |

The `tuvl.agent.*` counter names are unchanged by the agent unification (only
the span names moved from `autonomous_agent.*` to `agent.*`).

One additional counter lives on the `tuvl.hooks` meter:

| Counter | Incremented when | Attributes |
|---|---|---|
| `tuvl.hook.events` | A `type: hook` artifact with `action: metric` fires | `tuvl.hook.name`, `tuvl.hook.event`, `tuvl.workflow.name` |

A sustained rise in `tuvl.agent.aborts` or `tuvl.agent.judge_failures` is the
signal to inspect the run traces (`agent.iteration` spans) or the
Insight Agents dashboard.

---

## 7. Configuration Reference

All settings are read from `.env` or environment variables. Settings also support a per-project YAML override file at `.tuvl/telemetry.yaml` (see below).

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `TUVL_TELEMETRY_ENABLED` | `true` | Set `false` to disable all OTel export |
| `TUVL_OTLP_ENDPOINT` | `http://localhost:4317` | gRPC OTLP collector endpoint |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Standard OTel env var — takes precedence over `TUVL_OTLP_ENDPOINT` when set |
| `TUVL_SERVICE_NAME` | `tuvl` | `service.name` resource attribute in every span |
| `TUVL_ENV` | `production` | `production` → JSON logs; `development` → coloured console logs |

### `.tuvl/telemetry.yaml` override

Values from this file are applied only when the corresponding environment variable is not set, making it suitable for checked-in defaults that operators can override at deployment time.

```yaml
kind: TelemetryConfig
version: v1
spec:
  enabled: true
  otlp_endpoint: http://otel-collector:4317
  service_name: my-tuvl-app
```

### Config resolution order

For each setting, the first non-empty source wins:

1. Environment variable (`TUVL_TELEMETRY_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `TUVL_OTLP_ENDPOINT`, `TUVL_SERVICE_NAME`)
2. `.tuvl/telemetry.yaml` spec fields
3. Hardcoded defaults (enabled=`true`, endpoint=`http://localhost:4317`, service=`tuvl`)

---

## 8. Collector Setup

tuvl exports spans over **gRPC** to any OTLP-compatible collector. Below are quick-start configurations for common backends.

### OpenTelemetry Collector (recommended)

```yaml
# docker-compose.yml
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    ports:
      - "4317:4317"   # gRPC OTLP
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
      grpc:
        endpoint: 0.0.0.0:4317

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
TUVL_OTLP_ENDPOINT=http://localhost:4317
TUVL_SERVICE_NAME=my-app
```

### Grafana Tempo (via Alloy)

```bash
TUVL_OTLP_ENDPOINT=http://alloy:4317
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
TUVL_OTLP_ENDPOINT=http://datadog-agent:4317
```

---

## 9. Dev Mode Behaviour

When `TUVL_DEV_MODE=true` (set automatically by `tuvl dev`):

- `init_telemetry()` is a **no-op** — no `TracerProvider` is installed
- The OTel SDK default `NonRecordingSpan` is returned for all `start_as_current_span` calls — all span operations become no-ops with negligible overhead
- The LiteLLM OTel callback is **not** registered
- Logs use the **coloured ConsoleRenderer** instead of JSON
- `trace_id` / `span_id` are **not** injected into log events (no valid span context)

You can enable production-style JSON logging in dev by setting `TUVL_ENV=production` without changing `TUVL_DEV_MODE`.

---

## 10. Data Masking & PII

Context snapshots attached to spans (`tuvl.context.snapshot`) are scrubbed before export:

1. **Private keys stripped** — any context key starting with `_` (e.g. `_session`, `_db_handle`) is removed entirely
2. **Secure fields masked** — fields declared `secure: true` in any `ModelDefinition` are replaced with `"*****"`

```yaml
# models/employee.yaml
spec:
  fields:
    - name: national_id
      type: string
      secure: true        # → always "****" in spans and test snapshots
    - name: salary
      type: numeric
      secure: true
```

The same masking is applied to the Tuvl Spectrum execution trace, so PII never appears in developer tooling either.

The masking registry (`SECURE_FIELDS`) is populated at startup from all loaded `ModelDefinition` files and is additive — fields from all models contribute to a single global set.
