# invoke-external-api
description: Call external HTTP APIs and extract JSON data into context during a workflow.
options:
  argument-hint: "[api-url]"

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
