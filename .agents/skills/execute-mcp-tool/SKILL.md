# execute-mcp-tool
description: Call a Model Context Protocol (MCP) server tool via stdio or SSE.
options:
  argument-hint: "[tool-name]"

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
