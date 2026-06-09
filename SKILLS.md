# TUVL Framework Skills for AI Agents

The following skills represent standard procedural knowledge for building components within the TUVL framework. Use these patterns to map business requirements into correct declarative YAML steps.

---
name: create-database-model
description: Define a new entity to be stored in the PostgreSQL database with auto-generated APIs.
options:
  argument-hint: "[model-name]"
---

### Body

1. Create a new YAML file in `models/` (e.g., `models/customer.yaml`).
2. Add the document envelope:
   ```yaml
   kind: ModelDefinition
   metadata:
     name: Customer
     schema_version: v1
   enabled: true
   ```
3. Add the `spec:` block with `tablename:` and `fields:`.
4. Define the primary key: `{ name: id, type: uuid, primary_key: true, default: uuid4, input: false }`.
5. Specify the `type` for other fields. Allowed PostgreSQL field types are:
   - **Strings**: `string`, `text`, `varchar`
   - **Numbers**: `integer`, `bigint`, `smallint`, `numeric`, `float`
   - **Booleans & Identifiers**: `boolean`, `uuid`
   - **Dates/Times**: `date`, `timestamp`, `timestamptz`
   - **Complex/Binary**: `jsonb`, `bytea`
   - **Enum**: `enum` (always use this for categorical fields, and provide `enum_values: [val1, val2]`)
6. Use `input: false` for server-generated fields like `created_at`.
7. **Note:** TUVL auto-generates `/models/customer/...` CRUD routes automatically unless `schema: false` is defined.

---
name: implement-api-endpoint
description: Create a custom HTTP endpoint containing business logic via a TUVL Workflow.
options:
  argument-hint: "[workflow-name]"
---

### Body

1. Create a YAML file in `workflows/` (e.g., `workflows/process_order.yaml`).
2. Define the header:
   ```yaml
   kind: Workflow
   metadata:
     name: process_order
   enabled: true
   ```
3. Set `spec.context.models` to list all `ModelDefinition` references required in the workflow. **Omitting a model here causes a PermissionError.**
4. Define `spec.trigger` including `path`, `method`, `input_schema`, and `response_schema`.
5. List steps sequentially in `spec.steps`.
6. **Crucial:** Map all emitted signals (e.g., `default`, `error`) in the `routes:` map of every step. Terminate flow using `END`.

---
name: create-custom-python-node
description: Add custom Python logic that cannot be achieved via built-in YAML step kinds.
options:
  argument-hint: "[runner-name]"
---

### Body

1. Identify the runner name (e.g., `calculate_tax`).
2. Create exactly **one** file in `nodes/` named precisely as the runner (e.g., `nodes/calculate_tax.py`).
3. Import the node decorator and implement the logic:
   ```python
   from tuvl.core.nodes.base import node
   
   @node("calculate_tax")
   async def calculate_tax(ctx: dict) -> dict:
       amount = float(ctx.get("amount", 0))
       return {**ctx, "tax": amount * 0.2}
   ```
4. Invoke it in a workflow using `kind: functional` and `runner: calculate_tax`.

---
name: perform-database-operations
description: Create, Read, Update, Delete, or List database records during a workflow.
options:
  allowed-tools: [write_file]
---

### Body

1. Guarantee the model is whitelisted in the workflow's `spec.context.models`.
2. Add a workflow step of `kind: model-op`.
3. Set `model:` to the PascalCase model name.
4. Set `operation:` (`create`, `read`, `update`, `delete`, `list`).
5. Use `payload:` for writes, injecting context variables via `{{ var_name }}`.
6. Set `output:` to a context key name to store the returned data.
7. Explicitly route `default` and `error` signals.

---
name: implement-llm-agent-step
description: Use an LLM inside a workflow for text generation, extraction, or routing.
options:
  argument-hint: "[agent-model]"
---

### Body

1. Add a step of `kind: agent` to the workflow.
2. Define the `agent:` block:
   ```yaml
   - id: classify_text
     kind: agent
     agent:
       model: default # References llms/default.yaml or an inline LiteLLM string
       system: "You are a classifier."
       prompt: "Classify: {{ input_text }}"
       output:
         format: json
         map:
           category: text_category
     routes:
       default: next_step
       error: END
   ```
3. To inject external context (like DataSearch RAG results), utilize the `context_injection: [context_key]` array.

---
name: invoke-external-api
description: Call external HTTP APIs and extract JSON data into context during a workflow.
options:
  argument-hint: "[api-url]"
---

### Body

1. Add a step of `kind: api_call` to the workflow.
2. Define the `http:` configuration, interpolating variables with `{{ }}`:
   ```yaml
   - id: fetch_data
     kind: api_call
     http:
       url: "https://api.example.com/v1/data/{{ user_id }}"
       method: GET
       headers:
         Authorization: "Bearer {{ api_token }}"
       timeout: 30
     response:
       output_key: raw_response
       extract:
         - path: data.items.0.value # Dot-path syntax for JSON traversal
           as: first_item_value
     routes:
       default: next_step
       error: error_handler
   ```
3. The extracted variables become immediately available in the workflow context.

---
name: execute-mcp-tool
description: Call a Model Context Protocol (MCP) server tool via stdio or SSE.
options:
  argument-hint: "[tool-name]"
---

### Body

1. Add a step of `kind: mcp` to the workflow.
2. For an **SSE transport**, configure the `url` and `tool`:
   ```yaml
   - id: search_mcp
     kind: mcp
     mcp:
       transport: sse
       url: http://localhost:3001/sse
       tool: search
       arguments:
         query: "{{ user_query }}"
     response:
       output_key: mcp_result
     routes:
       default: next_step
       error: END
   ```
3. For local **stdio transports**, define `command` and `args` instead of a URL:
   ```yaml
   mcp:
     transport: stdio
     command: npx
     args: ["@modelcontextprotocol/server-github"]
     tool: list_issues
     env:
       GITHUB_TOKEN: "{{ github_token }}"
   ```
