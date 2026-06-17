# perform-database-operations
description: Create, Read, Update, Delete, or List database records during a workflow.
options:
  allowed-tools: [write_file]

### Body

1. Guarantee the model is whitelisted in the workflow's `spec.context.models`.
2. Add a workflow step of `kind: model-op`.
3. Set `model:` to the PascalCase model name.
4. Set `operation:` (`create`, `read`, `update`, `delete`, `list`).
5. Use `payload:` for writes, injecting context variables via `{{ var_name }}`.
6. Set `output:` to a context key name to store the returned data.
7. Explicitly route `default` and `error` signals.
