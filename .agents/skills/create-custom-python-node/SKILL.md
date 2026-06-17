# create-custom-python-node
description: Add custom Python logic that cannot be achieved via built-in YAML step kinds.
options:
  argument-hint: "[runner-name]"

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
