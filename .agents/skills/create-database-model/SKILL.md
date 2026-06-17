# create-database-model
description: Define a new entity to be stored in the PostgreSQL database with auto-generated APIs.
options:
  argument-hint: "[model-name]"

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
7. **Note:** TUVL auto-generated CRUD routes automatically unless `schema: false` is defined.
