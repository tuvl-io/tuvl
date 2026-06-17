# implement-llm-agent-step
description: Use an LLM inside a workflow for text generation, extraction, or routing.
options:
  argument-hint: "[agent-model]"

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
