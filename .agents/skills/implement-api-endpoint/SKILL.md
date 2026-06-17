# implement-api-endpoint
description: Create a custom HTTP endpoint containing business logic via a TUVL Workflow.
options:
  argument-hint: "[workflow-name]"

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
